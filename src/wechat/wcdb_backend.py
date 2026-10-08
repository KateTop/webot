"""
WCDB Native Backend — zero external dependencies.

Reads WeChat messages directly from the encrypted WCDB database via
patched wcdb_api.dll (ctypes).  Uses WeChatWindowController for sending.

WCDB native access — reads encrypted database in-process, no external dependencies.
"""
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from .base import AbstractWeChatBackend, MessageCallback
from .wcdb_client import WcdbNativeClient
from .window_controller import WeChatWindowController
from .helpers import DedupSet

logger = logging.getLogger(__name__)

DEFAULT_POLL_SEC = 1.0
MAX_DEDUP_SIZE = 5000
MAX_CONSECUTIVE_ERRORS = 5   # trigger reinit after this many consecutive failures
HISTORY_PAGE_SIZE = 200


class WcdbBackend(AbstractWeChatBackend):
    """Native WCDB backend — database read + window send.

    Reads messages directly from WeChat's session.db via wcdb_api.dll
    with one-byte DRM patch. Sends via WeChatWindowController.

    Usage:
        backend = WcdbBackend(
            bot_display_name="机器人",
            groups=["摸鱼群"],
            poll_sec=1.0,
        )
        backend.start(my_callback)
    """

    def __init__(self,
                 bot_display_name: str = "",
                 groups: list[str] | None = None,
                 poll_sec: float = DEFAULT_POLL_SEC,
                 store=None,
                 config=None,
                 on_history_ready=None):
        self._bot_name = bot_display_name
        self._groups = groups or []
        self._poll_sec = poll_sec
        self._store = store  # MessageStore fallback for name resolution
        self._confirmation_lock = threading.Lock()
        self._last_confirmed_id = {}
        self._running = False
        self._stop_requested = False
        self._client: Optional[WcdbNativeClient] = None
        if config is not None and getattr(config, "wechat_backend", "wcdb") == "wcdb_pywechat":
            from .pywechat_controller import PyWeChatSendController
            self._window = PyWeChatSendController()
        else:
            self._window = WeChatWindowController()
        self._talker_ids: dict[str, str] = {}
        self._known_ids = DedupSet(max_size=MAX_DEDUP_SIZE)
        # Thread safety: WCDB DLL (ctypes) may not be thread-safe internally.
        # All _client calls are serialized through this lock.
        self._client_lock = threading.Lock()
        # Foreground windows and clipboard are process-global: only one
        # callback may navigate/paste/send at a time.
        self._send_lock = threading.Lock()
        # Callback thread pool — fire-and-forget AI calls so the poll loop
        # never blocks on a slow summarization.
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        # Voice recognition pipeline (lazy-init when voice_asr_enabled)
        self._voice: Optional[object] = None
        self._voice_config = config
        self._on_history_ready = on_history_ready
        self._history_pool: concurrent.futures.ThreadPoolExecutor | None = None

    # ── Public API ─────────────────────────────────────────────────

    def start(self, callback: MessageCallback) -> None:
        self._stop_requested = False
        if not self._groups:
            logger.error("No groups configured. Set WECHAT_GROUPS in .env")
            return

        logger.info(
            "WcdbBackend starting (groups=%s, poll=%ss, bot=%r)",
            self._groups, self._poll_sec, self._bot_name,
        )

        # Init and open database
        try:
            self._client = WcdbNativeClient()
            self._client.init()
            self._client.open()
            logger.info("WCDB database opened successfully")
        except Exception as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to initialize WCDB: %s", e)
            # If init() allocated DLL/WCDB engine but open() failed,
            # clean up native resources so repeated retries don't leak.
            if self._client is not None:
                try:
                    self._client.close()
                    self._client = None
                except Exception:
                    pass
            try:
                from src.web.server import update_status
                update_status(running=False, error=str(e))
            except Exception:
                pass
            return

        # Resolve group talker IDs
        self._resolve_groups()

        if not self._talker_ids:
            logger.error("No groups resolved. Check WECHAT_GROUPS.")
            self._client.close()
            self._client = None
            return

        # Import missed messages before the live poll starts. This path only
        # writes to the store and never invokes the reply callback.
        for group_name, talker in self._talker_ids.items():
            if self._stop_requested:
                self._client.close()
                self._client = None
                return
            try:
                self._backfill_group(group_name, talker)
            except Exception:
                logger.exception("History import failed for %s", talker)
        if self._stop_requested:
            self._client.close()
            self._client = None
            return

        # Pre-find WeChat window
        hwnd = self._window.find_hwnd()
        if hwnd:
            logger.info("WeChat window pre-detected: HWND=%s", hwnd)
        else:
            logger.warning("WeChat window not found — will retry on first send")

        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="bot-cb-",
        )
        if self._on_history_ready:
            self._history_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="memory-history-",
            )
            for talker in self._talker_ids.values():
                self._history_pool.submit(self._on_history_ready, talker)
        self._running = True
        consecutive_errors = 0

        # Import once to avoid per-iteration overhead
        from src.web.server import is_shutting_down as _is_shutting_down

        try:
            while self._running and not _is_shutting_down():
                try:
                    self._poll_cycle(callback)
                    consecutive_errors = 0
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    consecutive_errors += 1

                    # After MAX_CONSECUTIVE_ERRORS consecutive failures,
                    # attempt full reinitialization (WeChat may have restarted).
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        logger.error(
                            "Hit %d consecutive errors — attempting "
                            "reinitialization...", consecutive_errors,
                        )
                        try:
                            self._reinitialize()
                            consecutive_errors = 0
                            continue
                        except Exception as reinit_err:
                            logger.error(
                                "Reinitialization failed: %s", reinit_err,
                            )
                            # Fall through to backoff; will retry next cycle.
                            push_error = str(reinit_err)
                            try:
                                from src.web.server import update_status
                                update_status(error=push_error)
                            except Exception:
                                pass

                    wait = min(2 ** min(consecutive_errors % MAX_CONSECUTIVE_ERRORS, 5), 30)
                    logger.warning(
                        "Poll error #%d (%s): %s. Retry in %ss...",
                        consecutive_errors, type(e).__name__, e, wait,
                    )
                    time.sleep(wait)
        finally:
            # Drain in-flight callbacks gracefully
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
            if self._history_pool:
                self._history_pool.shutdown(wait=False, cancel_futures=True)
                self._history_pool = None
            if self._client:
                self._client.close()
        logger.info("WcdbBackend stopped.")

    def send_text(self, chat_id: str, content: str) -> bool:
        if not content:
            return False

        group_name = self._talker_to_name(chat_id)
        if not group_name:
            logger.error("Cannot resolve chat_id=%s to group name", chat_id)
            if self._store is not None:
                self._store.record_send_failure(chat_id, chat_id, content)
            return False

        return self._send_and_confirm(group_name, chat_id, content)

    def stop(self) -> None:
        self._stop_requested = True
        self._running = False
        # The polling thread owns the executor and shuts it down in start()'s
        # finally block. Closing it here races with _poll_group.submit().

    def _backfill_group(self, group_name: str, talker: str) -> int:
        if self._store is None or self._client is None:
            return 0
        since_ts = self._store.get_latest_message_timestamp(talker)
        pages = []
        offset = 0
        reached_boundary = False
        while not self._stop_requested:
            with self._client_lock:
                batch = self._client.get_messages(
                    talker=talker, limit=HISTORY_PAGE_SIZE, offset=offset,
                )
            if not batch:
                break
            for raw in batch:
                try:
                    ts = int(raw.get("create_time", raw.get("createTime", 0)))
                except (ValueError, TypeError):
                    continue
                if since_ts is not None and ts < since_ts:
                    reached_boundary = True
                    continue
                pages.append(raw)
            offset += len(batch)
            if offset % 5000 == 0:
                logger.info("History scan for %s: %d messages examined", talker, offset)
            if reached_boundary or len(batch) < HISTORY_PAGE_SIZE:
                break
        imported = 0
        # WCDB returns newest first. Insert oldest first so the memory cursor
        # sees every row in the same order, including history added later.
        for raw in reversed(pages):
            standardized = self._standardize(raw, group_name, talker,
                                             historical=True)
            if (standardized is not None
                    and (not self._bot_name
                         or self._bot_name not in standardized["sender_name"])
                    and self._store.insert_message(standardized)):
                imported += 1
        logger.info("History import for %s: %d new messages, %d scanned",
                    talker, imported, offset)
        return imported

    # ── Recovery ─────────────────────────────────────────────────────

    def _reinitialize(self) -> None:
        """Close and re-open the WCDB client after persistent errors.

        Called when the poll loop hits MAX_CONSECUTIVE_ERRORS consecutive
        failures — typically because WeChat was restarted and the DB handle
        or HWND became stale.
        """
        logger.warning("Reinitializing WCDB backend after consecutive errors...")
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        try:
            self._client = WcdbNativeClient()
            self._client.init()
            self._client.open()
            logger.info("WCDB reinitialized successfully")
        except Exception as e:
            logger.error("WCDB reinitialization failed: %s", e)
            raise
        # Clear dedup set — WCDB may return messages with new IDs
        self._known_ids = DedupSet(max_size=MAX_DEDUP_SIZE)
        # Re-resolve groups (talker IDs may have changed)
        self._resolve_groups()
        # Re-find WeChat window
        hwnd = self._window.find_hwnd()
        if hwnd:
            logger.info("WeChat window re-detected: HWND=%s", hwnd)
        else:
            logger.warning("WeChat window not found after reinit")

    # ── Group resolution ────────────────────────────────────────────

    def _resolve_groups(self) -> None:
        """Map configured group names to talker IDs from WCDB sessions.

        WCDB session records only contain usernames (e.g. 20968749111@chatroom).
        Display names must be resolved via the DLL's get_display_names() or
        the local nickname cache (WeChat contacts / manual overrides).
        """
        sessions = self._client.get_sessions()

        # Build a map of all @chatroom entries: username -> {name, member_count}
        all_chatrooms: dict[str, dict] = {}
        for s in sessions:
            username = str(s.get("username", "") or "")
            if not username.endswith("@chatroom"):
                continue

            # Try session-level display name fields (rarely populated)
            display = str(
                s.get("displayName") or s.get("displayname")
                or s.get("nickname") or s.get("display_name")
                or ""
            ).strip()
            if not display:
                # Fall back to DLL lookup (resolves via contacts DB + nicknames)
                display = self._client.resolve_nickname(username)
            if not display or display == username:
                # Last resort: try last_sender_display_name from session,
                # or use the numeric prefix of username as label
                display = str(s.get("last_sender_display_name", "") or "").strip()
                if not display or display == username:
                    display = username  # fallback

            # Get real member count from group member list
            member_count = 0
            try:
                members = self._client.get_group_members(username)
                if members:
                    member_count = len(members)
            except Exception:
                pass

            all_chatrooms[username] = {
                "name": display,
                "member_count": member_count,
            }

        if not all_chatrooms:
            logger.error(
                "No @chatroom sessions found in WCDB (total sessions: %d). "
                "Make sure WeChat is logged in and session.db is accessible.",
                len(sessions),
            )
            return

        auto_discover = (
            not self._groups
            or (len(self._groups) == 1 and self._groups[0].strip() in ("*", "all", ""))
        )

        if auto_discover:
            for username, info in all_chatrooms.items():
                self._talker_ids[info["name"]] = username
            logger.info(
                "Auto-discovered %d group chats: %s",
                len(self._talker_ids), list(self._talker_ids.keys()),
            )
            self._groups = list(self._talker_ids.keys())

        else:
            # Manual mode: match configured names against resolved display names
            for group_name in self._groups:
                found = None
                for username, info in all_chatrooms.items():
                    display = info["name"]
                    if group_name.lower() in display.lower() or display.lower() in group_name.lower():
                        found = username
                        break
                if found:
                    self._talker_ids[group_name] = found
                    logger.info("Resolved '%s' -> %s (display='%s')", group_name, found, all_chatrooms[found]["name"])
                else:
                    # Direct lookup: maybe group_name IS a username like 20968749111@chatroom
                    if group_name in all_chatrooms:
                        self._talker_ids[group_name] = group_name
                        logger.info("Resolved '%s' as direct username", group_name)
                    else:
                        logger.warning(
                            "Could not resolve group '%s'. Available: %s",
                            group_name, list(all_chatrooms.keys()),
                        )

        # Persist chat_id -> display_name so the web UI can show
        # human-readable group names in the nickname dropdown.
        if all_chatrooms:
            self._save_group_names(all_chatrooms)

        # ── Resolve and persist group members ──────────────────────────
        group_members: dict[str, dict[str, str]] = {}
        for username, info in all_chatrooms.items():
            try:
                members = self._client.get_group_members(username)
                if not members:
                    continue
                wxids = [m.get("username", "") for m in members if m.get("username")]
                if not wxids:
                    continue
                # Resolve display names in batches of 200
                names = {}
                for i in range(0, len(wxids), 200):
                    batch = wxids[i:i + 200]
                    names.update(self._client.get_display_names(batch))
                # Filter out unresolved (where name == wxid) and save
                group_members[username] = {
                    wxid: names.get(wxid, wxid)
                    for wxid in wxids
                }
                logger.info(
                    "Resolved %d/%d member names for %s",
                    len(group_members[username]), len(wxids), info["name"],
                )
            except Exception as e:
                logger.warning("Failed to resolve members for %s: %s", username, e)
        if group_members:
            self._save_group_members(group_members)

    @staticmethod
    def _save_group_members(chat_members: dict[str, dict[str, str]]) -> None:
        """Persist chat_id -> {wxid: display_name} to data/group_members.json."""
        import os as _os
        path = Path("data/group_members.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            data = json.dumps(chat_members, ensure_ascii=False, indent=2)
            tmp_path.write_text(data, encoding="utf-8")
            _os.replace(tmp_path, path)
            total = sum(len(m) for m in chat_members.values())
            logger.info("Saved %d member names across %d groups to %s", total, len(chat_members), path)
        except Exception as e:
            logger.warning("Failed to persist group_members.json: %s", e)

    @staticmethod
    def _save_group_names(chatrooms: dict[str, dict]) -> None:
        """Persist chat_id -> {name, member_count} to data/group_names.json atomically."""
        import os as _os
        path = Path("data/group_names.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            data = json.dumps(chatrooms, ensure_ascii=False, indent=2)
            tmp_path.write_text(data, encoding="utf-8")
            _os.replace(tmp_path, path)
            logger.info(
                "Saved %d group-name mappings to %s",
                len(chatrooms), path,
            )
        except Exception as e:
            logger.warning("Failed to persist group_names.json: %s", e)

    def _talker_to_name(self, talker_id: str) -> str:
        for name, tid in self._talker_ids.items():
            if tid == talker_id:
                return name
        return ""

    # ── Message polling ──────────────────────────────────────────────

    def _poll_cycle(self, callback: MessageCallback) -> None:
        for group_name in list(self._groups):
            if not self._running:
                break
            talker = self._talker_ids.get(group_name)
            if not talker:
                continue
            self._poll_group(group_name, talker, callback)
        # Check shutdown signal before sleeping so stop() is responsive
        if not self._running:
            return
        time.sleep(self._poll_sec)

    def _poll_group(self, group_name: str, talker: str,
                    callback: MessageCallback) -> None:
        """Fetch messages for one group and dispatch new ones.

        AI-triggering callbacks are submitted to the thread pool so slow
        summarization in one group never blocks polling of other groups.
        """
        with self._client_lock:
            messages = self._client.get_messages(talker=talker, limit=50)
        if not messages:
            return

        for msg in reversed(messages):
            if not self._running:
                break

            standardized = self._standardize(msg, group_name, talker)
            if standardized is None:
                continue

            msg_id = standardized["message_id"]
            if msg_id in self._known_ids:
                continue
            self._known_ids.add(msg_id)

            own_id = (getattr(self._client, "_config", None) or {}).get("myWxid", "")
            if isinstance(own_id,str) and own_id and standardized.get("sender_id") == own_id:
                continue

            self._trim_dedup()

            # Fire-and-forget: callback (potentially AI call) + send run in
            # a thread pool worker so the poll loop continues immediately.
            pool = self._pool
            if not self._running or pool is None:
                return
            try:
                pool.submit(
                    self._handle_message,
                    group_name, talker, standardized, callback,
                )
            except RuntimeError as exc:
                # Interpreter shutdown can invalidate the executor even
                # though the poll flag has not yet been cleared.
                if "cannot schedule new futures after" not in str(exc):
                    raise
                logger.info("Callback executor closed; stopping WCDB poll")
                self._stop_requested = True
                self._running = False
                return

    def _handle_message(self, group_name: str, talker: str,
                        standardized: dict, callback: MessageCallback) -> None:
        """Execute callback and send reply (runs in thread pool worker)."""
        if not self._running:
            return

        try:
            cb_start = time.monotonic()
            reply = callback(standardized)
            cb_elapsed = time.monotonic() - cb_start
            if cb_elapsed > 0.5:
                logger.debug(
                    "Callback took %.2fs (msg_id=%s, group='%s')",
                    cb_elapsed, standardized["message_id"], group_name,
                )

            if reply:
                logger.info(
                    "Reply ready: group='%s' sender='%s' len=%d",
                    group_name, standardized["sender_name"], len(reply),
                )
                # _send_and_confirm uses window_controller (keyboard), not
                # _client (WCDB).  Don't hold _client_lock during send —
                # it blocks the poll loop from reading new messages.
                from src.monitor import monitor
                monitor.event("执行发送", {"text":reply,"native_mention":bool(standardized.get("reply_mention"))}, standardized.get("monitor_trace"))
                success = self.send_confirmed_text(talker, reply,
                    mention=standardized.get("reply_mention"), action=standardized.get("reply_action", ""))
                callback_result = getattr(self, "on_delivery", None)
                if callable(callback_result):
                    callback_result(standardized, success, self._last_confirmed_id.get(talker, ""))
                if success:
                    logger.info(
                        "Reply keyboard action completed: group='%s' (%d chars)",
                        group_name, len(reply),
                    )
                else:
                    logger.error(
                        "Reply FAILED: group='%s' — check WeChat window",
                        group_name,
                    )
        except Exception:
            from src.monitor import monitor
            monitor.event("发送或回调失败", "请检查发送记录与运行日志", standardized.get("monitor_trace"), status="失败")
            logger.exception(
                "Unhandled error in callback worker (group='%s', sender='%s')",
                group_name, standardized.get("sender_name", "?"),
            )

    # ── Voice recognition helpers ────────────────────────────────────

    def _get_voice(self):
        """Lazy-init the VoicePipeline (avoids import unless enabled)."""
        if self._voice is not None:
            return self._voice
        if self._voice_config is None:
            self._voice = False  # Sentinel: no config → disabled
            return False
        try:
            from src.voice import VoicePipeline
            self._voice = VoicePipeline(self._voice_config)
        except Exception:
            logger.exception("VoicePipeline init failed — voice disabled")
            self._voice = False
        return self._voice

    def _try_voice(self, msg: dict) -> Optional[str]:
        """Attempt voice recognition; return text or None on failure."""
        voice = self._get_voice()
        if not voice:  # False or None → disabled
            return None
        try:
            return voice.process(msg)
        except Exception:
            logger.exception("VoicePipeline.process failed")
            return None

    # ── Message standardization ──────────────────────────────────────

    def _standardize(self, msg: dict, group_name: str,
                     talker: str, historical: bool = False) -> Optional[dict]:
        """Convert WCDB raw message to standard format."""
        # WCDB message fields: sender_username, message_content, local_type, create_time
        sender = str(msg.get("sender_username", msg.get("senderUsername", msg.get("sender", ""))))
        content = str(msg.get("message_content", msg.get("content", ""))).strip()
        local_type = int(msg.get("localType", msg.get("msg_type", 1)))
        from .quote import inspect_quote
        own_wxid = (getattr(self._client, "_config", None) or {}).get("myWxid", "")
        quote = inspect_quote(content, own_wxid) if local_type == 49 else None
        quotes_bot = quote.quotes_this_account if quote else False
        quoted_content = quote.quoted_text if quote else ""
        if local_type == 49:
            logger.debug(
                "App message inspected: quote_valid=%s own_id_available=%s "
                "quotes_bot=%s mentions_bot_id=%s has_new_text=%s",
                quote.valid, bool(own_wxid), quotes_bot,
                quote.mentions_this_account, bool(quote.new_text),
            )
            if quote.valid:
                content = quote.new_text or ("[引用了你的消息]" if quotes_bot else "")
            elif "<msg" in content or "&lt;msg" in content:
                # Never search raw app XML: an @ in the quoted old message
                # would otherwise look like a new mention.
                return None

        # ── Voice recognition ──────────────────────────────────────
        # Voice messages (localType=34) have empty message_content;
        # we must recognise them BEFORE the empty-content check below.
        if local_type == 34:
            voice_text = None if historical else self._try_voice(msg)
            if voice_text:
                content = f"[语音] {voice_text}"
            else:
                content = "[语音]"
        elif not content:
            return None

        # ── System message handling ───────────────────────────────
        # Extract "xxx joined the group" events → welcome feature.
        # Only active when WELCOME_ENABLED=true; otherwise join messages
        # are silently filtered out like other system messages.
        _JOIN_PATTERN = re.compile(r'"([^"]+)"(?:通过[^"]*)?加入了群聊')
        join_match = _JOIN_PATTERN.search(content)
        new_member_id: str = ""
        is_system_join: bool = False
        welcome_on = (
            self._voice_config is not None
            and getattr(self._voice_config, "welcome_enabled", False)
        )
        if join_match and welcome_on:
            new_member_id = join_match.group(1)
            is_system_join = True
            logger.info(
                "Join event detected: new_member=%s group=%s",
                new_member_id, group_name[:20],
            )
        elif join_match:
            # Welcome disabled — silently drop join messages
            return None

        # Filter other system messages (but NOT join events)
        if not is_system_join:
            _FILTER_KEYWORDS = (
                "修改群名", "退出了群聊",
                "撤回了一条消息", "被移除", "开启了朋友验证",
                "移出了群聊",
            )
            # NOTE: "邀请" intentionally NOT in this list — it would
            # false-positive filter normal chat like "我邀请你参加活动".
            # System invite messages ("xxx邀请yyy加入了群聊") are already
            # caught by _JOIN_PATTERN above.
            if any(kw in content for kw in _FILTER_KEYWORDS):
                return None

        # Parse timestamp
        ts = msg.get("create_time", msg.get("createTime", msg.get("timestamp", 0)))
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            ts = int(time.time())

        # Resolve sender display name
        sender_name = self._client.resolve_nickname(sender)

        # Fallback: if WCDB DLL can't resolve (user not in contacts),
        # try the messages table for a previously seen display name
        if sender_name == sender and self._store is not None:
            prev = self._store.get_sender_display_name(sender)
            if prev:
                sender_name = prev

        # Resolve @mentions in content
        resolved_content = content
        if "@" in content:
            def _replace_at(match):
                at_wxid = match.group(0)[1:]
                name = self._client.resolve_nickname(at_wxid)
                return f"@{name}" if name != at_wxid else match.group(0)
            resolved_content = re.sub(r'@wxid_[a-zA-Z0-9]+', _replace_at, content)

        # Detect @mention of bot
        is_at = self._bot_name and (
            f"@{self._bot_name}" in resolved_content
            or f"@{self._bot_name}" in content
        )
        is_at = bool(is_at or (quote and quote.mentions_this_account))
        if local_type == 49 and (quotes_bot or is_at):
            logger.info(
                "Quote trigger classified: quotes_bot=%s at_bot=%s own_id_available=%s",
                bool(quotes_bot), bool(is_at), bool(own_wxid),
            )

        # Generate stable message ID.
        # For system join messages, use a content-based ID to avoid
        # volatile WCDB server_id/local_id causing dedup misses.
        if is_system_join:
            raw_id = f"join|{talker}|{new_member_id}|{content}|{ts}"
        else:
            server_id = str(msg.get("server_id", "") or "")
            local_id = str(msg.get("local_id", "") or "")
            raw_id = (server_id if server_id not in ("", "0") else local_id if local_id not in ("", "0") else f"{sender}|{content}|{ts}")
        msg_id = hashlib.md5(str(raw_id).encode()).hexdigest()

        return {
            "message_id": msg_id,
            "chat_id": talker,
            "group_name": group_name,
            "sender_id": str(sender),
            "sender_name": str(sender_name),
            "content": resolved_content,
            "msg_type": int(msg.get("localType", msg.get("msg_type", 1))),
            "timestamp": ts,
            "is_at_mentioned": is_at,
            "quotes_bot": quotes_bot,
            "quoted_content": quoted_content[:500] if quotes_bot else "",
            "is_group": True,
            "is_self": bool(own_wxid and sender == own_wxid),
            "is_system_join": is_system_join,
            "new_member_id": new_member_id,
        }

    def _trim_dedup(self) -> None:
        """DedupSet handles this internally."""
        pass

    # ── Message sending ──────────────────────────────────────────────

    def _send_and_confirm(self, group_name: str, talker: str,
                          content: str, record_failure: bool = True,
                          mention: tuple[str, str] | None = None) -> bool:
        """Send via WeChatWindowController (fire-and-forget).

        Returns True if the keyboard send action completed successfully.
        No confirmation polling — the window controller already retries
        on failure, and polling WCDB adds 3s of latency for marginal gain.
        """
        try:
            with self._send_lock:
                if mention:
                    success = self._window.send_to_chat(
                        group_name, content, mention=mention, chat_id=talker,
                    )
                else:
                    success = self._window.send_to_chat(group_name, content)
        except Exception:
            logger.exception("Send raised for group '%s'", group_name)
            success = False
        if not success and record_failure and self._store is not None:
            try:
                self._store.record_send_failure(talker, group_name, content)
            except Exception:
                logger.exception("Could not persist failed send for group '%s'", group_name)
        return success

    @staticmethod
    def _raw_key(row):
        return (str(row.get("sender_username", row.get("senderUsername", ""))),
                str(row.get("server_id", "")),str(row.get("local_id", "")),
                str(row.get("create_time", "")),str(row.get("message_content", row.get("content", ""))))

    def send_confirmed_text(self, chat_id, content, mention=None, action=""):
        """Serial send + bounded WCDB echo confirmation. Never retry an unconfirmed send."""
        from src.conversation_policy import load_policy
        from src.memory.workspace import MemoryWorkspace
        import random
        if not hasattr(self, "_confirmation_lock"):
            # Set during construction in production.
            self._confirmation_lock = threading.Lock()
        if not hasattr(self, "_last_confirmed_id"):
            self._last_confirmed_id = {}
        with self._confirmation_lock:
            if not self._running:
                return False
            self._last_confirmed_id.pop(chat_id, None)
            own_id = (getattr(self._client, "_config", None) or {}).get("myWxid", "")
            with self._client_lock:
                before = self._client.get_messages(talker=chat_id, limit=100)
            baseline = {self._raw_key(r) for r in before}
            started = time.time()
            policy = load_policy()
            time.sleep(random.uniform(policy["send_delay_min_sec"], policy["send_delay_max_sec"]))
            if not self._running:
                return False
            sent = self._send_and_confirm(self._talker_to_name(chat_id) or chat_id,chat_id,content,mention=mention)
            match = None
            if sent and own_id:
                deadline = time.monotonic() + 12
                while self._running and time.monotonic() < deadline:
                    with self._client_lock:
                        rows = self._client.get_messages(talker=chat_id, limit=100)
                    for row in rows:
                        sender = str(row.get("sender_username",row.get("senderUsername", "")))
                        body = str(row.get("message_content",row.get("content", ""))).strip()
                        if body.startswith(own_id+":\n"):
                            body = body[len(own_id)+2:]
                        normal = lambda value: re.sub(r"[\u2005\u200b\ufeff]", " ", value.replace("\r\n", "\n")).strip()
                        timestamp = float(row.get("create_time",row.get("timestamp",0)) or 0)
                        if sender == own_id and self._raw_key(row) not in baseline and timestamp >= started-2 and normal(body) == normal(content):
                            match = row
                            break
                    if match:
                        break
                    time.sleep(0.5)
            message_id = ""
            if match:
                server_id = str(match.get("server_id", "") or "")
                local_id = str(match.get("local_id", "") or "")
                raw_id = server_id if server_id not in ("", "0") else local_id if local_id not in ("", "0") else f"{own_id}|{content}|{int(started)}"
                message_id = hashlib.md5(raw_id.encode()).hexdigest()
            if self._store is not None:
                MemoryWorkspace(self._store)
                with self._store._lock, self._store.conn:
                    # Body is retained only when observed in the actual account's chat database.
                    self._store.conn.execute("INSERT INTO assistant_outbox(chat_id,content,action,status,message_id,created_at) VALUES(?,?,?,?,?,?)",
                        (chat_id,content if match else "",action,"confirmed" if match else "unconfirmed" if sent else "failed",message_id,started))
            if match:
                if self._store is not None:
                    self._store.insert_message(dict(message_id=message_id,chat_id=chat_id,
                        sender_id="__assistant__",sender_name=self._bot_name or "我",content=content,
                        timestamp=int(float(match.get("create_time",started))),msg_type=1))
                self._last_confirmed_id[chat_id] = message_id
            return match is not None

    def retry_failed_send(self, chat_id: str, content: str) -> bool:
        """Retry an already persisted item without creating another item."""
        group_name = self._talker_to_name(chat_id)
        if not group_name:
            return False
        return self._send_and_confirm(group_name, chat_id, content,
                                      record_failure=False)
