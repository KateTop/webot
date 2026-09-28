"""
Zero-dependency web UI server for the bot dashboard.

Uses only Python stdlib (http.server for HTTP + WebSocket).
Serves the React UI from ui/dist/ and provides bot status via WebSocket.

Runs in a daemon thread — no impact on the main bot loop.
"""
import json
import logging
import os
import struct
import threading
import time
import uuid
from hashlib import sha1
from base64 import b64encode
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlparse as _urlparse, parse_qs as _parse_qs

# Re-exported from config.py for use in API handlers.
# NOTE: _decode_wechat_groups is also imported inside _handle_request()
# conditional blocks, but for Python's scoping those later imports still
# make the name a local — so it must be imported at module level too,
# otherwise the first branch that references it raises UnboundLocalError.
from src.config import _decode_wechat_groups

logger = logging.getLogger(__name__)


def _message_db_path() -> str:
    """Read only the configured database path; never expose env values."""
    from src.config import find_env_file
    env_path = find_env_file()
    if env_path and env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DB_PATH="):
                return line.split("=", 1)[1].strip().strip('"\'') or "data/messages.db"
    return "data/messages.db"


def _failure_store():
    from src.db import initialize_db, MessageStore
    conn = initialize_db(_message_db_path())
    return conn, MessageStore(conn)

import sys as _sys
if getattr(_sys, "frozen", False):
    UI_DIR = (Path(_sys._MEIPASS) / "ui" / "dist").resolve()
else:
    UI_DIR = (Path(__file__).resolve().parent.parent.parent / "ui" / "dist").resolve()
WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _messages_table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    return row is not None


def _find_or_create_env() -> Path:
    """Find .env, or create it at the canonical location from config.py.

    If .env is not found but .env.example is, copy it to create a new .env.
    The created file ALWAYS goes to resolve_env_file() — never CWD-relative —
    so reads and writes can't drift apart.
    """
    import sys

    # 1. Use the canonical search from config.py (consistent across the app)
    from src.config import find_env_file, resolve_env_file
    existing = find_env_file()
    if existing:
        return existing

    # 2. Not found — create at the canonical path.
    env_path = resolve_env_file()

    # .env.example is bundled into _MEIPASS in frozen mode.
    if getattr(sys, "frozen", False):
        env_example = Path(sys._MEIPASS) / ".env.example"
    else:
        env_example = Path(__file__).resolve().parent.parent.parent / ".env.example"

    if env_example.exists():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("Created .env from .env.example at %s", env_path.resolve())
        return env_path

    # 3. Last resort: create minimal .env
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        "AI_BACKEND=deepseek\n"
        "DEEPSEEK_API_KEY=\n"
        "WECHAT_BACKEND=wcdb\n"
        "BOT_DISPLAY_NAME=\n"
        "WECHAT_GROUPS=\n",
        encoding="utf-8",
    )
    logger.info("Created minimal .env at %s", env_path.resolve())
    return env_path


def _detect_default_data_dir() -> str:
    """Auto-detect the default WeChat data directory (parent of wxid_*).

    Returns the base directory path string, or empty string if not found.
    Used by the UI to show what auto-detection would use.
    """
    import os as _os
    candidates = [
        Path(_os.environ.get("USERPROFILE", "")) / "Documents" / "xwechat_files",
        Path(_os.environ.get("USERPROFILE", "")) / "Documents" / "WeChat Files",
    ]
    for base in candidates:
        if not base.exists():
            continue
        try:
            wxid_dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith("wxid_")]
            for wxid_dir in wxid_dirs:
                session_db = wxid_dir / "db_storage" / "session" / "session.db"
                if session_db.exists():
                    return str(base)
        except PermissionError:
            continue
    return ""


def _mask_key(value: str) -> str:
    """Mask a sensitive key: show first 4 + last 4 chars, or '***' if too short."""
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


def _int_env(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float_env(raw: str, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _detect_wxid_and_db_path():
    """Auto-detect WeChat wxid and database path from common locations.

    Respects WECHAT_DATA_DIR env var as a custom base dir (scanned first).
    """
    import os as _os

    candidates: list[Path] = []

    # 1. Custom path from env (highest priority)
    custom_dir = _os.environ.get("WECHAT_DATA_DIR", "").strip()
    if custom_dir:
        custom = Path(custom_dir)
        if custom.exists() and custom.is_dir():
            candidates.append(custom)

    # 2. Default locations
    candidates += [
        Path(_os.environ.get("USERPROFILE", "")) / "Documents" / "xwechat_files",
        Path(_os.environ.get("USERPROFILE", "")) / "Documents" / "WeChat Files",
    ]
    for base in candidates:
        if not base.exists():
            continue
        wxid_dirs = sorted(
            [d for d in base.iterdir() if d.is_dir() and d.name.startswith("wxid_")],
            key=lambda d: d.stat().st_mtime, reverse=True,
        )
        for wxid_dir in wxid_dirs:
            session_db = wxid_dir / "db_storage" / "session" / "session.db"
            if session_db.exists():
                return wxid_dir.name, str(session_db)
            # Older WeChat versions
            msg_dir = wxid_dir / "Msg"
            if msg_dir.exists():
                db_files = sorted(msg_dir.glob("MSG*.db"), key=lambda f: f.stat().st_mtime, reverse=True)
                if db_files:
                    return wxid_dir.name, str(db_files[0])
    return None, None


def _set_env_key(env_path: Path, key: str, value: str) -> None:
    """Set or update one key=value in a .env file atomically."""
    _update_env(env_path, {key: value})


def _update_env(env_path: Path, updates: dict) -> list[str]:
    """Serialize a complete read-modify-write; retain unknown keys and secrets."""
    with _env_write_lock:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        # Masked export values must never replace stored credentials.
        safe = {k: str(v) for k, v in updates.items()
                if v is not None and not (isinstance(v, str) and "***" in v
                                      and ("KEY" in k or "SECRET" in k))}
        if any("\n" in value or "\r" in value for value in safe.values()):
            raise ValueError("配置值不能包含换行符")
        seen = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            key = stripped.split("=", 1)[0].strip() if stripped and not stripped.startswith("#") and "=" in stripped else None
            if key in safe:
                new_lines.append(f"{key}={safe[key]}")
                seen.add(key)
            else:
                new_lines.append(line)
        for key, value in safe.items():
            if key not in seen:
                new_lines.append(f"{key}={value}")
        tmp = env_path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        os.replace(tmp, env_path)
        return list(safe)


def _submitted_config_updates(config: dict, updates: dict) -> dict:
    """A settings page may submit only its own fields; preserve all others."""
    return {key: value for key, value in updates.items()
            if key.lower() in config and value is not None}


def _validate_ai_numeric_settings(config: dict) -> None:
    for field, low, high in (("ai_retry_count", 0, 10),
                             ("chat_context_count", 1, 100)):
        if field in config:
            try:
                value = int(config[field])
            except (TypeError, ValueError):
                raise ValueError(f"{field} 必须填写 {low}–{high} 的整数") from None
            if not low <= value <= high:
                raise ValueError(f"{field} 必须填写 {low}–{high} 的整数")


def _write_onboarding_to_env(env_path):
    """Write accumulated onboarding data to .env file atomically."""
    with _onboarding_lock:
        env_map = {
            "AI_BACKEND": _onboarding_data.get("ai_backend", "deepseek"),
            "DEEPSEEK_API_KEY": _onboarding_data.get("deepseek_api_key", ""),
            "DEEPSEEK_BASE_URL": _onboarding_data.get("deepseek_base_url", "https://api.deepseek.com"),
            "DEEPSEEK_MODEL": _onboarding_data.get("deepseek_model", "deepseek-v4-flash"),
            "OPENAI_API_KEY": _onboarding_data.get("openai_api_key", ""),
            "OPENAI_BASE_URL": _onboarding_data.get("openai_base_url", "https://api.openai.com/v1"),
            "OPENAI_MODEL": _onboarding_data.get("openai_model", "gpt-4o-mini"),
            "ANTHROPIC_API_KEY": _onboarding_data.get("anthropic_api_key", ""),
            "ANTHROPIC_BASE_URL": _onboarding_data.get("anthropic_base_url", "https://api.anthropic.com"),
            "SUMMARIZE_MODEL": _onboarding_data.get("summarize_model", "claude-haiku-4-5-20251001"),
            "WECHAT_BACKEND": _onboarding_data.get("wechat_backend", "wcdb"),
            "WECHAT_GROUPS": _onboarding_data.get("wechat_groups", "*"),
            "BOT_DISPLAY_NAME": _onboarding_data.get("bot_display_name", "群聊小助手"),
            "PROACTIVE_ENABLED": str(_onboarding_data.get("proactive_enabled", False)).lower(),
            "STICKY_MENTION_ENABLED": str(_onboarding_data.get("sticky_mention_enabled", True)).lower(),
            "WCDB_KEY": _onboarding_data.get("key", ""),
            "ONBOARDING_DONE": "true",
        }
    source_keys = {
        "AI_BACKEND": "ai_backend", "DEEPSEEK_API_KEY": "deepseek_api_key",
        "DEEPSEEK_BASE_URL": "deepseek_base_url", "DEEPSEEK_MODEL": "deepseek_model",
        "OPENAI_API_KEY": "openai_api_key", "OPENAI_BASE_URL": "openai_base_url",
        "OPENAI_MODEL": "openai_model", "ANTHROPIC_API_KEY": "anthropic_api_key",
        "ANTHROPIC_BASE_URL": "anthropic_base_url", "SUMMARIZE_MODEL": "summarize_model",
        "WECHAT_BACKEND": "wechat_backend", "WECHAT_GROUPS": "wechat_groups",
        "BOT_DISPLAY_NAME": "bot_display_name", "PROACTIVE_ENABLED": "proactive_enabled",
        "STICKY_MENTION_ENABLED": "sticky_mention_enabled", "WCDB_KEY": "key",
    }
    with _onboarding_lock:
        provided = {key: env_map[key] for key, source in source_keys.items()
                    if source in _onboarding_data}
    provided["ONBOARDING_DONE"] = "true"
    _update_env(env_path, provided)
    logger.info("Onboarding complete — wrote .env")


def _run_step1_extraction():
    """Background thread: wait for WeChat exit → restart → hook → capture.

    Uses extract_wcdb_key's on_progress callback to push real-time phase
    updates to the frontend so the user sees exactly what's happening.
    """
    # ── Ensure file logging is active during key extraction ──────────
    # setup_logging() normally runs inside Bot.run(), but key extraction
    # happens BEFORE the bot starts (during onboarding).  Without this,
    # all log output from extract_key.py goes to stdout only, which is
    # invisible in Windows GUI mode — making failures un-debuggable.
    from src.utils.logging_config import setup_logging
    from src.config import PROJECT_ROOT
    setup_logging(level="INFO", log_file=str(PROJECT_ROOT / "data" / "bot.log"))

    from src.wechat.extract_key import extract_wcdb_key

    def _on_progress(phase, message):
        """Push progress updates to the frontend via _step1_state."""
        with _step1_lock:
            _step1_state["phase"] = phase
            _step1_state["message"] = message

    try:
        # extract_wcdb_key(require_restart=True) handles the full flow.
        # on_progress pushes phase changes so the frontend can display
        # real-time instructions (hooking → waiting_exit → waiting_login
        # → hooking_restart).
        key = extract_wcdb_key(require_restart=True,
                               on_progress=_on_progress)

        if key:
            wxid, db_path = _detect_wxid_and_db_path()
            with _onboarding_lock:
                _onboarding_data["step1_done"] = True
                _onboarding_data["key"] = key
                _onboarding_data["wxid"] = wxid or ""
                _onboarding_data["db_path"] = db_path or ""

            # Persist the key to .env immediately so the bot can use it
            # on restart without needing to complete the full onboarding flow.
            env_path = _find_or_create_env()
            _set_env_key(env_path, "WCDB_KEY", key)
            # Also set in the current process for load_dotenv in this session
            import os as _os
            _os.environ["WCDB_KEY"] = key
            # Clear the KEY_MISSING error so it doesn't reappear on page refresh
            update_status(error="")

            with _step1_lock:
                _step1_state["phase"] = "done"
                _step1_state["message"] = "密钥获取成功"
                _step1_state["result"] = {"key": key, "wxid": wxid or "", "db_path": db_path or ""}
                _step1_state["running"] = False
        else:
            with _step1_lock:
                _step1_state["phase"] = "timeout"
                _step1_state["message"] = "密钥提取超时，请确保微信已登录并重试"
                _step1_state["running"] = False

    except Exception as e:
        logger.exception("Step1 extraction failed")
        with _step1_lock:
            _step1_state["phase"] = "error"
            _step1_state["message"] = str(e)
            _step1_state["running"] = False


def _list_dir_entries(target: Path) -> list[dict]:
    """List directory entries for the filesystem browser API.

    Returns only directories (the user is browsing for parent dir of wxid_*).
    Sorted: directories first, then alphabetically.
    """
    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith(".") or child.name.startswith("$"):
                continue  # skip hidden/system entries
            entries.append({
                "name": child.name,
                "path": str(child),
                "is_dir": child.is_dir(),
            })
    except PermissionError:
        pass
    return entries


def _read_recent_logs():
    """Read the last 500 lines from the bot log file. Returns JSON-serializable list.

    Log format: ``YYYY-MM-DD HH:MM:SS [LEVEL] module: message``
    (configured in src/utils/logging_config.py).
    """
    import re
    # CWD is set to app home by desktop.py; relative path works for both
    # frozen (EXE dir) and dev (project root) modes.
    log_path = Path("data/bot.log")
    if not log_path.exists():
        return {"ok": True, "logs": [], "message": "日志文件尚未创建"}
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        # Return last 500 lines
        recent = lines[-500:]
        # Regex: timestamp [LEVEL] module: message
        pattern = re.compile(
            r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+'
            r'\[(DEBUG|INFO|WARNING|ERROR)\]\s+'
            r'([^:]+):\s+'
            r'(.*)$'
        )
        entries = []
        for line in recent:
            entry = {"raw": line}
            m = pattern.match(line.strip())
            if m:
                entry["ts"] = m.group(1)
                entry["level"] = m.group(2)
                entry["module"] = m.group(3)
                entry["msg"] = m.group(4)
            else:
                # Fallback for lines that don't match (tracebacks, multi-line, etc.)
                entry["ts"] = ""
                entry["level"] = "INFO"
                entry["module"] = ""
                entry["msg"] = line
            entries.append(entry)
        return {"ok": True, "logs": entries}
    except Exception as e:
        return {"ok": False, "logs": [], "error": str(e)}


def _can_import(module_name: str) -> bool:
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False


def _platform_dependency_report(system_name=None, import_checker=None, command_checker=None):
    """Return platform-aware dependency diagnostics for onboarding."""
    import platform
    import shutil

    system = system_name or platform.system()
    import_checker = import_checker or _can_import
    command_checker = command_checker or shutil.which

    req_mapping = {
        "dotenv": "python-dotenv",
        "anthropic": "anthropic",
        "openai": "openai",
        "pydantic": "pydantic",
        "webview": "pywebview",
        "PIL": "Pillow",
        "psutil": "psutil",
        "pyperclip": "pyperclip",
    }
    if system == "Windows":
        req_mapping.update({
            "uiautomation": "uiautomation",
            "win32api": "pywin32",
            "comtypes": "comtypes",
        })

    missing_reqs = []
    for mod, pkg in req_mapping.items():
        if not import_checker(mod):
            missing_reqs.append(pkg)

    if system == "Darwin":
        # ddgs is a macOS-only dependency (DuckDuckGo integration)
        ddgs_ok = import_checker("ddgs") or import_checker("duckduckgo_search")
        if not ddgs_ok:
            missing_reqs.append("ddgs")

        for command in ("osascript", "pbcopy"):
            if not command_checker(command):
                missing_reqs.append(command)

    ok = len(missing_reqs) == 0
    value = "所有依赖已安装" if ok else f"缺少依赖: {', '.join(missing_reqs)}"
    return {"ok": ok, "value": value, "missing": missing_reqs}


def _platform_wechat_report(system_name=None):
    """Return a platform-aware WeChat process status."""
    import os as _os
    import platform
    import subprocess

    system = system_name or platform.system()
    if system == "Darwin":
        app_name = _os.getenv("MAC_WECHAT_APP_NAME", "WeChat")
        try:
            result = subprocess.run(
                ["pgrep", "-x", app_name],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                pid = result.stdout.strip().splitlines()[0]
                return {"ok": True, "value": f"微信运行中 (PID {pid})", "error": None}
            return {"ok": False, "value": "微信未运行", "error": "请启动 macOS 微信并授权辅助功能权限"}
        except Exception as e:
            return {"ok": False, "value": f"微信检测出错: {e}", "error": str(e)}

    try:
        from src.wechat.native.injector import _find_wechat_pid
        wx_pid, wx_name = _find_wechat_pid()
        wx_ok = wx_pid is not None
        wx_val = f"微信运行中 (PID {wx_pid})" if wx_ok else "微信未运行"
        return {"ok": wx_ok, "value": wx_val, "error": None if wx_ok else "请登录微信电脑端"}
    except Exception as e:
        return {"ok": False, "value": f"微信检测出错: {e}", "error": str(e)}


def _macos_wechat_diagnostics(system_name=None, automation=None):
    """Run macOS WeChat permission diagnostics from this process identity."""
    import platform

    system = system_name or platform.system()
    if system != "Darwin":
        return {
            "ok": False,
            "skipped": True,
            "error": "macOS diagnostics are only available on Darwin",
        }

    try:
        if automation is None:
            from src.wechat.mac_ui_backend import MacUIAutomation

            automation = MacUIAutomation()
        return automation.diagnose_access()
    except Exception as exc:
        logger.exception("macOS WeChat diagnostics failed")
        return {
            "ok": False,
            "skipped": False,
            "error": str(exc),
        }


# ── Thread-safe server state classes ────────────────────────────────────


class _ServerStatus:
    """Thread-safe bot status with WebSocket broadcast.

    All writes are serialized through an internal lock so concurrent
    update_status() calls from different threads never produce inconsistent
    status snapshots.
    """

    _FIELDS = (
        "running", "uptime_sec", "messages_processed",
        "wechat_backend", "ai_backend", "db_ok",
        "last_api_call_sec_ago", "last_api_call_time",
        "timestamp", "error",
    )

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.uptime_sec = 0
        self.messages_processed = 0
        self.wechat_backend = ""
        self.ai_backend = ""
        self.db_ok = False
        self.last_api_call_sec_ago = -1
        self.last_api_call_time = 0.0
        self.timestamp = ""
        self.error = ""
        self._clients: list = []
        self._clients_lock = threading.Lock()

    def update(self, **kwargs):
        """Update status fields and broadcast to all WebSocket clients."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            snapshot = self._snapshot_locked()
        self._broadcast(snapshot)

    def snapshot(self):
        """Return a consistent dict snapshot (thread-safe)."""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self):
        """Build a dict from fields (caller must hold _lock)."""
        return {k: getattr(self, k) for k in self._FIELDS}

    def add_client(self, sock):
        with self._clients_lock:
            self._clients.append(sock)

    def remove_client(self, sock):
        with self._clients_lock:
            if sock in self._clients:
                self._clients.remove(sock)

    def _broadcast(self, snapshot):
        """Push snapshot to all connected WebSocket clients."""
        payload = json.dumps(snapshot, ensure_ascii=False)
        dead = []
        with self._clients_lock:
            for sock in self._clients:
                try:
                    _send_ws_frame(sock, payload)
                except Exception:
                    dead.append(sock)
            for s in dead:
                if s in self._clients:
                    self._clients.remove(s)


class _BotControl:
    """Thread-safe bot lifecycle control.

    Serializes start/stop transitions so concurrent API requests cannot
    create duplicate bot instances or leave the state inconsistent.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.thread = None
        self.backend = None
        self.running = False

    def register(self, thread=None, backend=None):
        with self._lock:
            if thread is not None:
                self.thread = thread
            if backend is not None:
                self.backend = backend
            self.running = True

    def try_start(self, thread) -> bool:
        """Reserve the bot slot before launching its thread."""
        with self._lock:
            if self.running:
                return False
            self.thread = thread
            self.backend = None
            self.running = True
            return True

    def register_backend(self, backend):
        """Called by Bot.run() during initialization."""
        with self._lock:
            self.backend = backend

    def stop(self):
        """Stop the bot backend and wait for the thread to exit."""
        # Read refs under lock, then call stop + join outside the lock
        # to avoid deadlock if stop() needs the lock.
        with self._lock:
            backend = self.backend
            thread = self.thread

        if backend is not None and hasattr(backend, "stop"):
            backend.stop()

        if thread is not None and thread.is_alive():
            thread.join(timeout=30)

        if thread is not None and thread.is_alive():
            # A callback may still be draining. Keep the backend registered
            # so /api/start cannot overlap a second poller with this one.
            logger.warning("Bot thread is still stopping after 30s")
            return False

        with self._lock:
            if self.backend is backend and self.thread is thread:
                self.running = False
                self.backend = None
                self.thread = None
        return backend is not None

    def is_running(self):
        with self._lock:
            return self.running

    def set_running(self):
        with self._lock:
            self.running = True

    def mark_stopped(self):
        """Reset running state when the bot thread exits on its own.

        Does NOT stop the backend or join the thread — use stop() for
        external shutdown requests.  This is called from within the bot
        thread's ``finally`` block so the next /api/start can proceed.
        """
        with self._lock:
            self.running = False
            self.backend = None
            self.thread = None

    def set_thread(self, thread):
        with self._lock:
            self.thread = thread


class _ServerStartGuard:
    """Thread-safe idempotent server start guard."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started = False

    def try_start(self):
        """Return True if server should start, False if already started."""
        with self._lock:
            if self._started:
                return False
            self._started = True
            return True


# ── Module-level instances ────────────────────────────────────────────

_status = _ServerStatus()
_bot_control = _BotControl()
_env_write_lock = threading.Lock()  # serialize all .env writes across threads
_server_guard = _ServerStartGuard()
_shutdown_event = threading.Event()
_voice_downloads: dict[str, dict] = {}  # model → {active, msg}
_memory_runtime = None
_memory_runtime_lock = threading.Lock()
_memory_jobs: dict[str, dict] = {}
_memory_jobs_lock = threading.Lock()


def _register_memory_runtime(consolidator):
    global _memory_runtime
    with _memory_runtime_lock:
        _memory_runtime = consolidator


def _clear_memory_runtime():
    global _memory_runtime
    with _memory_runtime_lock:
        _memory_runtime = None


def _memory_worker(chat_id: str, end_id: int, job_id: str):
    conn = None
    try:
        with _memory_runtime_lock:
            worker = _memory_runtime
        if worker is None:
            from src.config import load_config
            from src.summarize import create_summarizer
            from src.memory.consolidator import MemoryConsolidator
            conn, store = _failure_store()
            worker = MemoryConsolidator(store, create_summarizer(load_config()))
        success = worker.consolidate_first_pending_segment(chat_id, end_id)
        with _memory_jobs_lock:
            _memory_jobs[job_id] = {
                "state": "done" if success else "failed",
                "error": "整理失败或正在进行，请查看运行日志后重试" if not success else "",
            }
    except Exception as exc:
        logger.exception("Manual memory consolidation failed")
        with _memory_jobs_lock:
            _memory_jobs[job_id] = {"state": "failed", "error": str(exc)}
    finally:
        if conn is not None:
            conn.close()


def signal_shutdown():
    """Signal all components to stop (called on app exit)."""
    _shutdown_event.set()


def is_shutting_down():
    """Check if shutdown has been signaled."""
    return _shutdown_event.is_set()

# ── Onboarding state ──────────────────────────────────────────────────

_onboarding_data = {
    "step1_done": False, "step2_done": False, "step3_done": False, "step4_done": False,
    "key": "", "wxid": "", "db_path": "",
    "bot_display_name": "", "wechat_groups": "*", "wechat_backend": "wcdb",
    "ai_backend": "deepseek", "deepseek_api_key": "", "deepseek_model": "deepseek-v4-flash",
    "anthropic_api_key": "", "summarize_model": "claude-haiku-4-5-20251001",
    "proactive_enabled": False,
    "sticky_mention_enabled": True,
}
_onboarding_lock = threading.Lock()

# Async step1 state
_step1_state = {
    "running": False,
    "phase": "idle",   # idle | waiting_exit | waiting_login | hooking | done | error
    "message": "",
    "result": None,    # {"key": ..., "wxid": ..., "db_path": ...}
}
_step1_thread = None
_step1_lock = threading.Lock()


# ── Public API wrappers (delegate to thread-safe classes) ─────────────


def update_status(**kwargs):
    """Push status update to all WebSocket clients (thread-safe)."""
    _status.update(**kwargs)


def register_bot(thread=None, backend=None):
    """Register bot thread/backend so the web API can control it."""
    _bot_control.register(thread=thread, backend=backend)
    update_status(running=True)


def _bot_exited():
    """Notify that the bot thread has exited (any path — normal/error).

    Resets the control lock so the next /api/start can proceed.
    Called from desktop.py's start_bot() and _start_bot_in_thread().
    """
    _bot_control.mark_stopped()
    _clear_memory_runtime()


def _register_backend(backend):
    """Register backend from Bot.run() — explicit API, no monkey-patching."""
    _bot_control.register_backend(backend)


def _stop_bot():
    """Stop the running bot backend. Returns True if anything was stopped."""
    stopped = _bot_control.stop()
    still_running = _bot_control.is_running()
    if not still_running:
        _clear_memory_runtime()
    update_status(running=still_running)
    if stopped:
        logger.info("Bot stopped via web API")
    return stopped


def _start_bot_in_thread():
    """Start the bot in a new daemon thread. Call from API handler."""
    import sys
    from src.config import PROJECT_ROOT

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    def _run():
        try:
            from src.config import load_config
            config = load_config()
            update_status(
                wechat_backend=config.wechat_backend,
                ai_backend=config.ai_backend,
                error="",
            )
            from src.bot import Bot
            bot = Bot(config)
            # Bot.run() calls _register_backend() during init — no patch needed
            bot.run()
        except SystemExit:
            update_status(running=False)
        except Exception as e:
            update_status(running=False, error=str(e))
            logger.exception("Bot crashed during startup")
        finally:
            # Always clear the running flag so the user can restart
            # (bot.run() exits gracefully on errors like KEY_MISSING)
            _bot_control.mark_stopped()
            _clear_memory_runtime()

    thread = threading.Thread(target=_run, daemon=True, name="bot-main")
    if not _bot_control.try_start(thread):
        return {"ok": False, "error": "Bot is already running"}
    update_status(running=True)
    try:
        thread.start()
    except RuntimeError as exc:
        _bot_control.mark_stopped()
        update_status(running=False, error=str(exc))
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def _recv_exactly(sock, n):
    """Receive exactly n bytes from a socket (handles TCP fragmentation)."""
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _send_ws_frame(sock, text):
    """Send a WebSocket text frame."""
    data = text.encode("utf-8")
    frame = bytearray()
    frame.append(0x81)  # FIN + text opcode
    if len(data) < 126:
        frame.append(len(data))
    elif len(data) < 65536:
        frame.append(126)
        frame.extend(struct.pack(">H", len(data)))
    else:
        frame.append(127)
        frame.extend(struct.pack(">Q", len(data)))
    frame.extend(data)
    sock.sendall(bytes(frame))


def _read_ws_frame(sock):
    """Read a WebSocket frame (handles TCP fragmentation)."""
    header = _recv_exactly(sock, 2)
    if header is None:
        return None
    opcode = header[0] & 0x0F
    if opcode == 0x8:  # close
        return None
    if opcode == 0x9:  # ping
        # Send pong
        pong = bytearray([0x8A, 0x00])  # FIN + pong opcode, no payload
        sock.sendall(bytes(pong))
        return b""  # return empty to keep reading
    length = header[1] & 0x7F
    if length == 126:
        ext = _recv_exactly(sock, 2)
        if ext is None:
            return None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = _recv_exactly(sock, 8)
        if ext is None:
            return None
        length = struct.unpack(">Q", ext)[0]
    mask = _recv_exactly(sock, 4)
    if mask is None:
        return None
    payload = _recv_exactly(sock, length)
    if payload is None:
        return None
    payload = bytearray(payload)
    for i in range(len(payload)):
        payload[i] ^= mask[i % 4]
    return bytes(payload)


def _handle_ws_upgrade(headers, conn):
    """Perform WebSocket handshake using already-parsed headers.

    Uses the ``http.client.HTTPMessage`` object directly — avoids re-parsing
    raw bytes, which broke on Python 3.13 where ``headers.as_bytes()`` no
    longer round-trips faithfully.
    """
    key = headers.get("Sec-WebSocket-Key", "")
    if not key:
        logger.warning("WS upgrade rejected: missing Sec-WebSocket-Key")
        return False

    accept = b64encode(sha1((key + WEBSOCKET_GUID.decode()).encode()).digest()).decode()

    conn.sendall(
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
    )
    logger.info("WS upgrade accepted")
    return True


class _UIHandler(SimpleHTTPRequestHandler):
    """HTTP handler: static files + WebSocket upgrade + API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self):
        # Only delegate specific API paths; return 405 for unknown POST paths
        if self.path in ("/api/config", "/api/config/import", "/api/prompts", "/api/start", "/api/stop",
                         "/api/memory/save", "/api/memory/create", "/api/memory/consolidate",
                         "/api/conversation-policy",
                         "/api/nicknames",
                         "/api/welcome/templates",
                         "/api/onboarding/reset",
                         "/api/onboarding/step1", "/api/onboarding/step2",
                         "/api/onboarding/step3", "/api/onboarding/step4",
                         "/api/sandbox/test",
                         "/api/send-failures/retry",
                         "/api/voice/download-model",
                         "/api/wechat-data-dir/detect"):
            self.do_GET()
        else:
            self.send_response(405)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "Method not allowed"}).encode())

    def do_GET(self):
        self._handle_request()

    def _handle_request(self):
        parsed_path = _urlparse(self.path)
        if parsed_path.path == "/api/conversation-policy":
            origin = self.headers.get("Origin", "")
            if (self.client_address[0] not in ("127.0.0.1", "::1")
                    or (origin and _urlparse(origin).hostname not in
                        ("127.0.0.1", "localhost", "::1"))):
                self.send_json({"ok": False, "error": "Local request required"})
                return
            from src.conversation_policy import load_policy, save_policy
            try:
                if self.command == "POST":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 10000:
                        raise ValueError("策略配置大小无效")
                    policy = save_policy(json.loads(self.rfile.read(length)))
                else:
                    policy = load_policy()
                self.send_json({"ok": True, "policy": policy})
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)})
            return
        if parsed_path.path.startswith("/api/memory"):
            origin = self.headers.get("Origin", "")
            if (self.client_address[0] not in ("127.0.0.1", "::1")
                    or (origin and _urlparse(origin).hostname not in
                        ("127.0.0.1", "localhost", "::1"))):
                self.send_json({"ok": False, "error": "Local request required"})
                return
            query = _parse_qs(parsed_path.query)
            conn = None
            try:
                if parsed_path.path == "/api/memory/jobs":
                    job_id = query.get("id", [""])[0]
                    with _memory_jobs_lock:
                        job = _memory_jobs.get(job_id)
                    self.send_json({"ok": job is not None, "job": job})
                    return
                if parsed_path.path in ("/api/memory/save", "/api/memory/create", "/api/memory/consolidate"):
                    if self.command != "POST":
                        raise ValueError("POST required")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 40000:
                        raise ValueError("请求大小无效")
                    payload = json.loads(self.rfile.read(length))
                    chat_id = str(payload.get("chat_id", "")).strip()
                    if not chat_id or len(chat_id) > 256:
                        raise ValueError("群 ID 无效")
                    if parsed_path.path == "/api/memory/create":
                        conn, store = _failure_store()
                        path = store.ensure_group_memory_file(chat_id)
                        self.send_json({"ok": True, "soul_path": str(path)})
                        return
                    if parsed_path.path == "/api/memory/save":
                        text_value = payload.get("text", "")
                        if not isinstance(text_value, str):
                            raise ValueError("soul.md 必须是文本")
                        with _memory_jobs_lock:
                            if any(job.get("chat_id") == chat_id and job["state"] == "running"
                                   for job in _memory_jobs.values()):
                                raise RuntimeError("该群正在手动整理记忆，请稍后再保存")
                            with _memory_runtime_lock:
                                runtime = _memory_runtime
                            if runtime is not None:
                                runtime.save_manual_text(chat_id, text_value)
                            else:
                                from src.memory.consolidator import MemoryConsolidator
                                conn, store = _failure_store()
                                MemoryConsolidator(store, None).save_manual_text(chat_id, text_value)
                        self.send_json({"ok": True})
                        return
                    end_id = int(payload.get("end_id", 0))
                    conn, store = _failure_store()
                    segments = store.list_pending_memory_segments(chat_id)["segments"]
                    if not segments or segments[0]["end_id"] != end_id:
                        raise ValueError("请先选择最早的待整理时间段，或刷新列表")
                    job_id = uuid.uuid4().hex
                    with _memory_jobs_lock:
                        if any(job.get("chat_id") == chat_id and job["state"] == "running"
                               for job in _memory_jobs.values()):
                            raise RuntimeError("该群已有手动整理任务，请等待完成")
                        _memory_jobs[job_id] = {"state": "running", "error": "", "chat_id": chat_id}
                        if len(_memory_jobs) > 100:
                            for key in list(_memory_jobs)[:50]:
                                if _memory_jobs[key]["state"] != "running":
                                    del _memory_jobs[key]
                    threading.Thread(target=_memory_worker,
                                     args=(chat_id, end_id, job_id), daemon=True).start()
                    self.send_json({"ok": True, "job_id": job_id})
                    return
                if self.command != "GET":
                    raise ValueError("GET required")
                conn, store = _failure_store()
                if parsed_path.path == "/api/memory/groups":
                    from src.config import PROJECT_ROOT
                    names_path = PROJECT_ROOT / "data" / "group_names.json"
                    names = json.loads(names_path.read_text(encoding="utf-8")) if names_path.exists() else {}
                    rows = conn.execute(
                        "SELECT chat_id FROM messages UNION SELECT chat_id FROM group_memory ORDER BY chat_id"
                    ).fetchall()
                    groups = []
                    known_ids = {row[0] for row in rows}
                    known_ids.update(key for key in names if key.endswith("@chatroom"))
                    for chat_id in sorted(known_ids):
                        info = names.get(chat_id, chat_id)
                        label = info.get("name", chat_id) if isinstance(info, dict) else str(info)
                        groups.append({"chat_id": chat_id, "group_name": label})
                    self.send_json({"ok": True, "groups": groups})
                    return
                chat_id = query.get("chat_id", [""])[0]
                if not chat_id:
                    raise ValueError("群 ID 不能为空")
                if parsed_path.path == "/api/memory/segment":
                    start_id = int(query.get("start_id", ["0"])[0])
                    end_id = int(query.get("end_id", ["0"])[0])
                    self.send_json({"ok": True, "messages":
                                    store.get_pending_segment_messages(chat_id, start_id, end_id)})
                    return
                if parsed_path.path == "/api/memory":
                    memory = store.get_group_memory(chat_id)
                    pending = store.list_pending_memory_segments(chat_id)
                    path = store._soul_path(chat_id)
                    self.send_json({"ok": True, "memory": memory,
                                    "pending": pending, "soul_path": str(path) if path else "",
                                    "soul_exists": bool(path and path.exists())})
                    return
                raise ValueError("Unknown memory endpoint")
            except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)})
            except Exception:
                logger.exception("Memory API failed")
                self.send_json({"ok": False, "error": "记忆操作失败，请查看日志"})
            finally:
                if conn is not None:
                    conn.close()
            return

        if self.path == "/api/send-failures/retry":
            if self.command != "POST":
                self.send_json({"ok": False, "error": "POST required"})
                return
            origin = self.headers.get("Origin", "")
            if (self.client_address[0] not in ("127.0.0.1", "::1")
                    or (origin and _urlparse(origin).hostname not in
                        ("127.0.0.1", "localhost", "::1"))):
                self.send_json({"ok": False, "error": "Local request required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1000:
                    raise ValueError("Invalid request size")
                failure_id = int(json.loads(self.rfile.read(length))["id"])
                if failure_id < 1:
                    raise ValueError("Invalid id")
                backend = _bot_control.backend
                if not _bot_control.is_running() or not hasattr(backend, "retry_failed_send"):
                    self.send_json({"ok": False, "error": "微信后端未运行或不支持重发"})
                    return
                conn, store = _failure_store()
                try:
                    item = store.claim_send_failure(failure_id)
                finally:
                    conn.close()
                if item is None:
                    self.send_json({"ok": False, "error": "记录不存在或正在重发"})
                    return
                try:
                    success = backend.retry_failed_send(item["chat_id"], item["content"])
                except Exception:
                    logger.exception("Manual resend failed for item %d", failure_id)
                    success = False
                conn, store = _failure_store()
                try:
                    store.finish_send_failure(failure_id, success)
                finally:
                    conn.close()
                self.send_json({"ok": success, "status": "sent" if success else "failed"})
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)})
            return

        if self.path == "/api/send-failures" or self.path.startswith("/api/send-failures?"):
            if self.command != "GET":
                self.send_json({"ok": False, "error": "GET required"})
                return
            origin = self.headers.get("Origin", "")
            if (self.client_address[0] not in ("127.0.0.1", "::1")
                    or (origin and _urlparse(origin).hostname not in
                        ("127.0.0.1", "localhost", "::1"))):
                self.send_json({"ok": False, "error": "Local request required"})
                return
            try:
                query = _parse_qs(_urlparse(self.path).query)
                page = int(query.get("page", ["1"])[0])
                page_size = int(query.get("page_size", ["20"])[0])
                status = query.get("status", ["failed"])[0]
                conn, store = _failure_store()
                try:
                    result = store.list_send_failures(page, page_size, status)
                finally:
                    conn.close()
                self.send_json({"ok": True, **result})
            except (ValueError, OSError) as exc:
                self.send_json({"ok": False, "error": str(exc)})
            return

        if self.path == "/api/prompts":
            from src.summarize.prompt_settings import load_prompt_settings, save_prompt_settings
            try:
                if self.command == "POST":
                    length = int(self.headers.get("Content-Length", 0))
                    if length > 30000:
                        raise ValueError("Prompt 配置过大")
                    data = json.loads(self.rfile.read(length) if length else b"{}")
                    prompts = save_prompt_settings(data)
                else:
                    prompts = load_prompt_settings()
                self.send_json({"ok": True, "prompts": prompts})
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)})
            return
        # ── WebSocket upgrade ─────────────────────────────────────────
        if self.path == "/ws":
            connection_header = self.headers.get("Connection", "").lower()
            upgrade_header = self.headers.get("Upgrade", "").lower()
            if "upgrade" in connection_header and upgrade_header == "websocket":
                if _handle_ws_upgrade(self.headers, self.request):
                    _status.add_client(self.request)
                    # Send initial status
                    try:
                        _send_ws_frame(
                            self.request,
                            json.dumps(_status.snapshot(), ensure_ascii=False),
                        )
                    except Exception:
                        _status.remove_client(self.request)
                        return
                    # Read loop (ping/pong handled in _read_ws_frame)
                    while True:
                        try:
                            frame = _read_ws_frame(self.request)
                            if frame is None:
                                break
                        except Exception:
                            break
                    _status.remove_client(self.request)
                    return
                else:
                    self.send_response(400)
                    self.end_headers()
                    return

        # ── API: Start bot ────────────────────────────────────────────
        if self.path == "/api/start":
            if _bot_control.is_running():
                self.send_json({"ok": True, "already_running": True})
            else:
                result = _start_bot_in_thread()
                self.send_json(result)
            return

        # ── API: Stop bot ─────────────────────────────────────────────
        if self.path == "/api/stop":
            _stop_bot()
            self.send_json({"ok": True})
            return

        # ── API: Load config ───────────────────────────────────────────
        if self.path == "/api/load-config":
            from src.config import _decode_wechat_groups  # noqa: F811 - needed before use (scoping)
            env_path = _find_or_create_env()
            raw = {}
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        raw[k.strip()] = v.strip()
            config_data = {
                "ai_backend": raw.get("AI_BACKEND", "deepseek"),
                "ai_fallback_order": raw.get("AI_FALLBACK_ORDER", ""),
                "ai_retry_count": _int_env(raw.get("AI_RETRY_COUNT", "3"), 3),
                "chat_context_count": _int_env(raw.get("CHAT_CONTEXT_COUNT", "30"), 30),
                "deepseek_api_key": _mask_key(raw.get("DEEPSEEK_API_KEY", "")),
                "deepseek_base_url": raw.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                "deepseek_model": raw.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                "openai_api_key": _mask_key(raw.get("OPENAI_API_KEY", "")),
                "openai_base_url": raw.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                "openai_model": raw.get("OPENAI_MODEL", "gpt-4o-mini"),
                "openai_web_search": raw.get("OPENAI_WEB_SEARCH", "false").lower() == "true",
                "custom_api_key": _mask_key(raw.get("CUSTOM_API_KEY", "")),
                "custom_base_url": raw.get("CUSTOM_BASE_URL", ""),
                "custom_model": raw.get("CUSTOM_MODEL", ""),
                "anthropic_api_key": _mask_key(raw.get("ANTHROPIC_API_KEY", "")),
                "anthropic_base_url": raw.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                "summarize_model": raw.get("SUMMARIZE_MODEL", "claude-haiku-4-5-20251001"),
                "bot_display_name": raw.get("BOT_DISPLAY_NAME", ""),
                "wechat_backend": raw.get("WECHAT_BACKEND", "wcdb"),
                "wechat_groups": _decode_wechat_groups(raw.get("WECHAT_GROUPS", "*")),
                "proactive_enabled": raw.get("PROACTIVE_ENABLED", "false").lower() == "true",
                "proactive_rate_window_sec": _int_env(raw.get("PROACTIVE_RATE_WINDOW_SEC", "120"), 120),
                "proactive_rate_quiet": _float_env(raw.get("PROACTIVE_RATE_QUIET", "1.5"), 1.5),
                "proactive_rate_casual": _float_env(raw.get("PROACTIVE_RATE_CASUAL", "4.0"), 4.0),
                "proactive_rate_lively": _float_env(raw.get("PROACTIVE_RATE_LIVELY", "6.5"), 6.5),
                "proactive_rate_burst": _float_env(raw.get("PROACTIVE_RATE_BURST", "8.5"), 8.5),
                "welcome_enabled": raw.get("WELCOME_ENABLED", "false").lower() == "true",
                "sticky_mention_enabled": raw.get("STICKY_MENTION_ENABLED", "true").lower() == "true",
                "sticky_mention_ttl_sec": _int_env(raw.get("STICKY_MENTION_TTL_SEC", "60"), 60),
                "summarize_enabled": raw.get("SUMMARIZE_ENABLED", "true").lower() == "true",
                "fallback_window_hours": _int_env(raw.get("FALLBACK_WINDOW_HOURS", "8"), 8),
                "trigger_keywords": [
                    kw.strip() for kw in raw.get("TRIGGER_KEYWORDS", "").split(",")
                    if kw.strip()
                ],
                "log_level": raw.get("LOG_LEVEL", "INFO"),
                "wechat_data_dir": raw.get("WECHAT_DATA_DIR", ""),
                "voice_asr_enabled": raw.get("VOICE_ASR_ENABLED", "false").lower() == "true",
                "voice_asr_backend": raw.get("VOICE_ASR_BACKEND", "local_whisper"),
                "voice_asr_language": raw.get("VOICE_ASR_LANGUAGE", "zh"),
                "voice_openai_api_key": _mask_key(raw.get("VOICE_OPENAI_API_KEY", "")),
                "voice_openai_base_url": raw.get("VOICE_OPENAI_BASE_URL", ""),
                "voice_local_model": raw.get("VOICE_LOCAL_MODEL", "small"),
            }
            self.send_json({
                "ok": True,
                "config": config_data,
                "detected_data_dir": _detect_default_data_dir(),
            })
            return

        # ── API: Export config ───────────────────────────────────────
        if self.path == "/api/config/export":
            from datetime import date as _dt_date
            try:
                env_path = _find_or_create_env()
                raw = {}
                if env_path.exists():
                    for line in env_path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            raw[k.strip()] = v.strip()
                export_data = {
                    "ai_backend": raw.get("AI_BACKEND", "deepseek"),
                    "ai_fallback_order": raw.get("AI_FALLBACK_ORDER", ""),
                    "ai_retry_count": _int_env(raw.get("AI_RETRY_COUNT", "3"), 3),
                    "chat_context_count": _int_env(raw.get("CHAT_CONTEXT_COUNT", "30"), 30),
                    "deepseek_api_key": _mask_key(raw.get("DEEPSEEK_API_KEY", "")),
                    "deepseek_base_url": raw.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                    "deepseek_model": raw.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                    "openai_api_key": _mask_key(raw.get("OPENAI_API_KEY", "")),
                    "openai_base_url": raw.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                    "openai_model": raw.get("OPENAI_MODEL", "gpt-4o-mini"),
                    "openai_web_search": raw.get("OPENAI_WEB_SEARCH", "false").lower() == "true",
                    "custom_api_key": _mask_key(raw.get("CUSTOM_API_KEY", "")),
                    "custom_base_url": raw.get("CUSTOM_BASE_URL", ""),
                    "custom_model": raw.get("CUSTOM_MODEL", ""),
                    "anthropic_api_key": _mask_key(raw.get("ANTHROPIC_API_KEY", "")),
                    "anthropic_base_url": raw.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                    "summarize_model": raw.get("SUMMARIZE_MODEL", "claude-haiku-4-5-20251001"),
                    "bot_display_name": raw.get("BOT_DISPLAY_NAME", ""),
                    "wechat_backend": raw.get("WECHAT_BACKEND", "wcdb"),
                    "wechat_groups": raw.get("WECHAT_GROUPS", "*"),
                        "proactive_enabled": raw.get("PROACTIVE_ENABLED", "false").lower() == "true",
                    "proactive_rate_window_sec": _int_env(raw.get("PROACTIVE_RATE_WINDOW_SEC", "120"), 120),
                    "proactive_rate_quiet": _float_env(raw.get("PROACTIVE_RATE_QUIET", "1.5"), 1.5),
                    "proactive_rate_casual": _float_env(raw.get("PROACTIVE_RATE_CASUAL", "4.0"), 4.0),
                    "proactive_rate_lively": _float_env(raw.get("PROACTIVE_RATE_LIVELY", "6.5"), 6.5),
                    "proactive_rate_burst": _float_env(raw.get("PROACTIVE_RATE_BURST", "8.5"), 8.5),
                    "welcome_enabled": raw.get("WELCOME_ENABLED", "false").lower() == "true",
                    "sticky_mention_enabled": raw.get("STICKY_MENTION_ENABLED", "true").lower() == "true",
                    "sticky_mention_ttl_sec": _int_env(raw.get("STICKY_MENTION_TTL_SEC", "60"), 60),
                    "summarize_enabled": raw.get("SUMMARIZE_ENABLED", "true").lower() == "true",
                    "fallback_window_hours": _int_env(raw.get("FALLBACK_WINDOW_HOURS", "8"), 8),
                    "trigger_keywords": [
                        kw.strip() for kw in raw.get("TRIGGER_KEYWORDS", "").split(",")
                        if kw.strip()
                    ],
                    "log_level": raw.get("LOG_LEVEL", "INFO"),
                    "wechat_data_dir": raw.get("WECHAT_DATA_DIR", ""),
                }
                filename = f"webot-config-{_dt_date.today().isoformat()}.json"
                body = json.dumps(export_data, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                logger.exception("Failed to export config")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Save config ──────────────────────────────────────────
        if self.path == "/api/config":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                config = json.loads(body)
                _validate_ai_numeric_settings(config)
                env_path = _find_or_create_env()
                updates = {
                    "DEEPSEEK_API_KEY": config.get("deepseek_api_key"),
                    "DEEPSEEK_BASE_URL": config.get("deepseek_base_url"),
                    "DEEPSEEK_MODEL": config.get("deepseek_model"),
                    "OPENAI_API_KEY": config.get("openai_api_key"),
                    "OPENAI_BASE_URL": config.get("openai_base_url"),
                    "OPENAI_MODEL": config.get("openai_model"),
                    "OPENAI_WEB_SEARCH": str(config.get("openai_web_search", False)).lower(),
                    "CUSTOM_API_KEY": config.get("custom_api_key"),
                    "CUSTOM_BASE_URL": config.get("custom_base_url"),
                    "CUSTOM_MODEL": config.get("custom_model"),
                    "ANTHROPIC_API_KEY": config.get("anthropic_api_key"),
                    "ANTHROPIC_BASE_URL": config.get("anthropic_base_url"),
                    "SUMMARIZE_MODEL": config.get("summarize_model"),
                    "AI_BACKEND": config.get("ai_backend"),
                    "AI_FALLBACK_ORDER": config.get("ai_fallback_order"),
                    "AI_RETRY_COUNT": config.get("ai_retry_count"),
                    "CHAT_CONTEXT_COUNT": config.get("chat_context_count"),
                    "BOT_DISPLAY_NAME": config.get("bot_display_name"),
                    "WECHAT_BACKEND": config.get("wechat_backend"),
                    "WECHAT_GROUPS": config.get("wechat_groups") or "*",
                    "PROACTIVE_ENABLED": str(config.get("proactive_enabled", False)).lower(),
                    "PROACTIVE_RATE_WINDOW_SEC": str(config.get("proactive_rate_window_sec", 120)),
                    "PROACTIVE_RATE_QUIET": str(config.get("proactive_rate_quiet", 1.5)),
                    "PROACTIVE_RATE_CASUAL": str(config.get("proactive_rate_casual", 4.0)),
                    "PROACTIVE_RATE_LIVELY": str(config.get("proactive_rate_lively", 6.5)),
                    "PROACTIVE_RATE_BURST": str(config.get("proactive_rate_burst", 8.5)),
                    "WELCOME_ENABLED": str(config.get("welcome_enabled", False)).lower(),
                    "STICKY_MENTION_ENABLED": str(config.get("sticky_mention_enabled", True)).lower(),
                    "STICKY_MENTION_TTL_SEC": str(config.get("sticky_mention_ttl_sec", 60)),
                    "SUMMARIZE_ENABLED": str(config.get("summarize_enabled", True)).lower(),
                    "FALLBACK_WINDOW_HOURS": str(config.get("fallback_window_hours", 8)),
                    "TRIGGER_KEYWORDS": ",".join(config.get("trigger_keywords") or []),
                    "LOG_LEVEL": config.get("log_level"),
                    "WECHAT_DATA_DIR": config.get("wechat_data_dir"),
                    "VOICE_ASR_ENABLED": str(config.get("voice_asr_enabled", False)).lower(),
                    "VOICE_ASR_BACKEND": config.get("voice_asr_backend", "local_whisper"),
                    "VOICE_ASR_LANGUAGE": config.get("voice_asr_language", "zh"),
                    "VOICE_OPENAI_API_KEY": config.get("voice_openai_api_key", ""),
                    "VOICE_OPENAI_BASE_URL": config.get("voice_openai_base_url", ""),
                    "VOICE_LOCAL_MODEL": config.get("voice_local_model", "small"),
                }
                updates = _submitted_config_updates(config, updates)
                # ── Safety: never overwrite real secrets with masked values.
                #     load-config returns masked keys (e.g. "sk-r***t-k"); the
                #     frontend sends them back unchanged.  Writing a masked
                #     string to .env permanently destroys the real secret.
                for masked_key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "CUSTOM_API_KEY",
                                   "ANTHROPIC_API_KEY",
                                   "VOICE_OPENAI_API_KEY"):
                    val = updates.get(masked_key)
                    if isinstance(val, str) and "***" in val:
                        updates[masked_key] = None  # skip → keep existing
                saved_keys = _update_env(env_path, updates)
                for key in saved_keys:
                    os.environ[key] = str(updates[key])
                self.send_json({
                    "ok": True,
                    "saved": saved_keys,
                    "requires_restart": True,
                })
            except Exception as e:
                logger.exception("Failed to save config")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Import config ─────────────────────────────────────────
        if self.path == "/api/config/import":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                config = json.loads(body)
                _validate_ai_numeric_settings(config)
                # Basic validation: must look like a webot config export
                expected_keys = ['ai_backend', 'deepseek_model', 'wechat_backend']
                has_keys = any(k in config for k in expected_keys)
                if not has_keys:
                    raise ValueError("无效的配置文件格式：缺少必需字段")
                env_path = _find_or_create_env()
                updates = {
                    "DEEPSEEK_API_KEY": config.get("deepseek_api_key"),
                    "DEEPSEEK_BASE_URL": config.get("deepseek_base_url"),
                    "DEEPSEEK_MODEL": config.get("deepseek_model"),
                    "OPENAI_API_KEY": config.get("openai_api_key"),
                    "OPENAI_BASE_URL": config.get("openai_base_url"),
                    "OPENAI_MODEL": config.get("openai_model"),
                    "OPENAI_WEB_SEARCH": str(config.get("openai_web_search", False)).lower(),
                    "CUSTOM_API_KEY": config.get("custom_api_key"),
                    "CUSTOM_BASE_URL": config.get("custom_base_url"),
                    "CUSTOM_MODEL": config.get("custom_model"),
                    "ANTHROPIC_API_KEY": config.get("anthropic_api_key"),
                    "ANTHROPIC_BASE_URL": config.get("anthropic_base_url"),
                    "SUMMARIZE_MODEL": config.get("summarize_model"),
                    "AI_BACKEND": config.get("ai_backend"),
                    "AI_FALLBACK_ORDER": config.get("ai_fallback_order"),
                    "AI_RETRY_COUNT": config.get("ai_retry_count"),
                    "CHAT_CONTEXT_COUNT": config.get("chat_context_count"),
                    "BOT_DISPLAY_NAME": config.get("bot_display_name"),
                    "WECHAT_BACKEND": config.get("wechat_backend"),
                    "WECHAT_GROUPS": config.get("wechat_groups") or "*",
                    "PROACTIVE_ENABLED": str(config.get("proactive_enabled", False)).lower(),
                    "PROACTIVE_RATE_WINDOW_SEC": str(config.get("proactive_rate_window_sec", 120)),
                    "PROACTIVE_RATE_QUIET": str(config.get("proactive_rate_quiet", 1.5)),
                    "PROACTIVE_RATE_CASUAL": str(config.get("proactive_rate_casual", 4.0)),
                    "PROACTIVE_RATE_LIVELY": str(config.get("proactive_rate_lively", 6.5)),
                    "PROACTIVE_RATE_BURST": str(config.get("proactive_rate_burst", 8.5)),
                    "WELCOME_ENABLED": str(config.get("welcome_enabled", False)).lower(),
                    "STICKY_MENTION_ENABLED": str(config.get("sticky_mention_enabled", True)).lower(),
                    "STICKY_MENTION_TTL_SEC": str(config.get("sticky_mention_ttl_sec", 60)),
                    "SUMMARIZE_ENABLED": str(config.get("summarize_enabled", True)).lower(),
                    "FALLBACK_WINDOW_HOURS": str(config.get("fallback_window_hours", 8)),
                    "TRIGGER_KEYWORDS": ",".join(config.get("trigger_keywords", [])) if config.get("trigger_keywords") else None,
                    "LOG_LEVEL": config.get("log_level"),
                    "WECHAT_DATA_DIR": config.get("wechat_data_dir"),
                    "VOICE_ASR_ENABLED": str(config.get("voice_asr_enabled", False)).lower(),
                    "VOICE_ASR_BACKEND": config.get("voice_asr_backend", "local_whisper"),
                    "VOICE_ASR_LANGUAGE": config.get("voice_asr_language", "zh"),
                    "VOICE_OPENAI_API_KEY": config.get("voice_openai_api_key", ""),
                    "VOICE_OPENAI_BASE_URL": config.get("voice_openai_base_url", ""),
                    "VOICE_LOCAL_MODEL": config.get("voice_local_model", "small"),
                }
                # Older exports omit newer fields. Do not reset those settings.
                updates = {key: value for key, value in updates.items()
                           if key.lower() in config}
                saved_keys = _update_env(env_path, updates)
                # Update in-process environment
                for key in saved_keys:
                    os.environ[key] = str(updates[key])
                self.send_json({
                    "ok": True,
                    "imported": saved_keys,
                    "requires_restart": True,
                })
            except ValueError as e:
                self.send_json({"ok": False, "error": str(e)})
            except Exception as e:
                logger.exception("Failed to import config")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Get nickname groups ─────────────────────────────────────
        if self.path == "/api/nicknames/groups":
            conn = None
            try:
                from src.config import find_env_file, _decode_wechat_groups
                env_path = find_env_file()
                import sqlite3

                # Resolve group names same way as wcdb_backend: read env, match sessions
                groups_raw = "*"
                if env_path and env_path.exists():
                    for line in env_path.read_text(encoding="utf-8").splitlines():
                        if line.strip().startswith("WECHAT_GROUPS="):
                            groups_raw = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                            break
                groups_raw = _decode_wechat_groups(groups_raw)

                db_path = "data/messages.db"
                if env_path and env_path.exists():
                    for line in env_path.read_text(encoding="utf-8").splitlines():
                        if line.strip().startswith("DB_PATH="):
                            db_path = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                            break

                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row

                # ── Load persisted chat_id -> group info ─────────────────
                group_names_path = Path("data/group_names.json")
                group_info: dict[str, dict] = {}
                if group_names_path.exists():
                    try:
                        raw = json.loads(group_names_path.read_text(encoding="utf-8"))
                        for chat_id, val in raw.items():
                            if isinstance(val, dict):
                                group_info[chat_id] = {
                                    "name": val.get("name", chat_id),
                                    "member_count": int(val.get("member_count", 0)),
                                }
                            else:
                                group_info[chat_id] = {
                                    "name": str(val),
                                    "member_count": 0,
                                }
                    except (json.JSONDecodeError, OSError):
                        pass

                groups = []
                if group_info:
                    for chat_id, info in group_info.items():
                        mc = info["member_count"]
                        if not mc:
                            cnt_row = conn.execute(
                                "SELECT COUNT(DISTINCT sender_id) FROM messages WHERE chat_id=?",
                                (chat_id,),
                            ).fetchone()
                            mc = cnt_row[0] if cnt_row else 0
                        groups.append({
                            "chat_id": chat_id,
                            "group_name": info["name"],
                            "member_count": mc,
                        })

                if _messages_table_exists(conn):
                    existing_ids = set(group_info.keys())
                    rows = conn.execute(
                        "SELECT DISTINCT chat_id FROM messages WHERE chat_id LIKE '%@chatroom%' ORDER BY chat_id"
                    ).fetchall()
                    for row in rows:
                        chat_id = row["chat_id"]
                        if chat_id in existing_ids:
                            continue
                        cnt_row = conn.execute(
                            "SELECT COUNT(DISTINCT sender_id) FROM messages WHERE chat_id=?",
                            (chat_id,),
                        ).fetchone()
                        groups.append({
                            "chat_id": chat_id,
                            "group_name": chat_id,
                            "member_count": cnt_row[0] if cnt_row else 0,
                        })

                self.send_json({"ok": True, "groups": groups})
            except Exception as e:
                logger.exception("Failed to list nickname groups")
                self.send_json({"ok": False, "error": str(e)})
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            return

        # ── API: Get nicknames for a group ────────────────────────────────
        if self.path.startswith("/api/nicknames") and self.command == "GET":
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path != "/api/nicknames":
                self.send_json({"ok": False, "error": "not found"})
                return
            try:
                chat_id = params.get("chat_id", [""])[0]
                if not chat_id:
                    self.send_json({"ok": False, "error": "missing chat_id"})
                    return

                from src.nickname import NicknameService
                nicks = NicknameService()
                overrides = nicks.load()

                # ── Collect all known wxids for this group ──
                seen: set[str] = set()
                member_map: dict[str, str] = {}  # wxid -> best display_name

                # 1) From messages table (people who have sent messages)
                import sqlite3
                from src.config import load_config
                config = load_config()
                conn = sqlite3.connect(config.db_path)
                conn.row_factory = sqlite3.Row
                if _messages_table_exists(conn):
                    rows = conn.execute(
                        "SELECT DISTINCT sender_id, sender_name FROM messages WHERE chat_id=? ORDER BY sender_name",
                        (chat_id,),
                    ).fetchall()
                    for row in rows:
                        wxid = row["sender_id"]
                        seen.add(wxid)
                        if row["sender_name"]:
                            member_map[wxid] = row["sender_name"]
                conn.close()

                # 2) From group_members.json (full member list from WCDB)
                gm_path = Path("data/group_members.json")
                if gm_path.exists():
                    try:
                        group_members = json.loads(gm_path.read_text(encoding="utf-8"))
                        chat_members = group_members.get(chat_id, {})
                        for wxid, display_name in chat_members.items():
                            if wxid not in seen:
                                seen.add(wxid)
                                member_map[wxid] = display_name
                    except (json.JSONDecodeError, OSError):
                        pass

                # 3) Build response
                members = [
                    {
                        "wxid": wxid,
                        "display_name": member_map.get(wxid, wxid),
                        "nickname": overrides.get(wxid, ""),
                    }
                    for wxid in seen
                ]

                self.send_json({"ok": True, "members": members})
            except Exception as e:
                logger.exception("Failed to get nicknames")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Save nickname ────────────────────────────────────────────
        if self.path == "/api/nicknames" and self.command != "GET":
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len) if content_len else b"{}"
                data = json.loads(body)
                wxid = (data.get("wxid") or "").strip()
                nickname = (data.get("nickname") or "").strip()

                if not wxid:
                    self.send_json({"ok": False, "error": "missing wxid"})
                    return

                from src.nickname import NicknameService
                nicks = NicknameService()
                if nickname:
                    nicks.update(wxid, nickname)
                else:
                    nicks.remove(wxid)

                self.send_json({"ok": True})
            except Exception as e:
                logger.exception("Failed to save nickname")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Get / Save welcome templates ─────────────────────────
        if self.path == "/api/welcome/templates":
            if self.command == "GET":
                try:
                    from src.welcome import get_welcome_manager
                    wm = get_welcome_manager()
                    data = wm.load()
                    self.send_json({"ok": True, "data": data})
                except Exception as e:
                    logger.exception("Failed to load welcome templates")
                    self.send_json({"ok": False, "error": str(e)})
            else:
                # POST — save welcome templates + group mappings
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len) if content_len else b"{}"
                try:
                    data = json.loads(body)
                    from src.welcome import get_welcome_manager
                    wm = get_welcome_manager()
                    wm.save(data)
                    self.send_json({"ok": True})
                except Exception as e:
                    logger.exception("Failed to save welcome templates")
                    self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Test AI prompt sandbox ──────────────────────────────
        if self.path == "/api/sandbox/test":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                data = json.loads(body)
                message = data.get("message", "").strip()
                sender_name = data.get("sender_name", "张三").strip()
                group_name = data.get("group_name", "技术交流群").strip()
                group_memory = data.get("group_memory", "").strip()
                context_messages = data.get("context_messages", [])

                # ── Apply frontend overrides to os.environ BEFORE
                #     load_config() so validation sees the sandbox values
                #     rather than whatever is (or isn't) in .env.
                sandbox_env_overrides = {}
                if data.get("ai_backend"):
                    sandbox_env_overrides["AI_BACKEND"] = data["ai_backend"]
                if data.get("deepseek_api_key"):
                    sandbox_env_overrides["DEEPSEEK_API_KEY"] = data["deepseek_api_key"]
                if data.get("deepseek_model"):
                    sandbox_env_overrides["DEEPSEEK_MODEL"] = data["deepseek_model"]
                if data.get("deepseek_base_url"):
                    sandbox_env_overrides["DEEPSEEK_BASE_URL"] = data["deepseek_base_url"]
                if data.get("openai_api_key"):
                    sandbox_env_overrides["OPENAI_API_KEY"] = data["openai_api_key"]
                if data.get("openai_model"):
                    sandbox_env_overrides["OPENAI_MODEL"] = data["openai_model"]
                if data.get("openai_base_url"):
                    sandbox_env_overrides["OPENAI_BASE_URL"] = data["openai_base_url"]
                for env_key, field in (("CUSTOM_API_KEY", "custom_api_key"),
                                       ("CUSTOM_BASE_URL", "custom_base_url"),
                                       ("CUSTOM_MODEL", "custom_model"),
                                       ("AI_RETRY_COUNT", "ai_retry_count")):
                    if data.get(field) is not None and data.get(field) != "":
                        sandbox_env_overrides[env_key] = data[field]
                if data.get("anthropic_api_key"):
                    sandbox_env_overrides["ANTHROPIC_API_KEY"] = data["anthropic_api_key"]
                if data.get("anthropic_base_url"):
                    sandbox_env_overrides["ANTHROPIC_BASE_URL"] = data["anthropic_base_url"]
                if data.get("summarize_model"):
                    sandbox_env_overrides["SUMMARIZE_MODEL"] = data["summarize_model"]

                # Load .env first, then temporarily apply sandbox overrides
                # to os.environ so load_config() picks them up.  Save and
                # restore original values to prevent cross-request pollution
                # (e.g. sandbox test key leaking into a subsequent /api/start).
                from dotenv import load_dotenv
                from src.config import find_env_file, load_config
                from src.summarize import create_summarizer

                env_path = find_env_file()
                if env_path:
                    load_dotenv(env_path, override=True)
                else:
                    load_dotenv(override=True)

                # Save originals, apply overrides
                _saved_env = {}
                for key, value in sandbox_env_overrides.items():
                    _saved_env[key] = os.environ.get(key)
                    os.environ[key] = str(value)
                try:
                    config = load_config()

                    # Apply remaining overrides to the config object directly
                    # (covers fields load_config() reads but doesn't apply from env).
                    if sandbox_env_overrides:
                        _apply_override = lambda k, attr: (
                            setattr(config, attr, sandbox_env_overrides[k])
                            if k in sandbox_env_overrides else None
                        )
                        _apply_override("AI_BACKEND", "ai_backend")
                        _apply_override("DEEPSEEK_API_KEY", "deepseek_api_key")
                        _apply_override("DEEPSEEK_MODEL", "deepseek_model")
                        _apply_override("DEEPSEEK_BASE_URL", "deepseek_base_url")
                        _apply_override("OPENAI_API_KEY", "openai_api_key")
                        _apply_override("OPENAI_MODEL", "openai_model")
                        _apply_override("OPENAI_BASE_URL", "openai_base_url")
                        _apply_override("CUSTOM_API_KEY", "custom_api_key")
                        _apply_override("CUSTOM_BASE_URL", "custom_base_url")
                        _apply_override("CUSTOM_MODEL", "custom_model")
                        _apply_override("ANTHROPIC_API_KEY", "anthropic_api_key")
                        _apply_override("ANTHROPIC_BASE_URL", "anthropic_base_url")
                        _apply_override("SUMMARIZE_MODEL", "summarize_model")
                finally:
                    # Restore original os.environ — prevent pollution
                    for key, orig in _saved_env.items():
                        if orig is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = orig

                if not message:
                    self.send_json({
                        "ok": False,
                        "error": "请输入测试消息内容",
                    })
                    return

                # Create summarizer
                summarizer = create_summarizer(config)

                # Call chat
                reply = summarizer.chat(
                    message=message,
                    context_messages=context_messages,
                    requester_name=sender_name,
                    bot_name=config.bot_display_name or "群聊小助手",
                    group_name=group_name,
                    group_memory=group_memory,
                )

                self.send_json({
                    "ok": True,
                    "reply": reply,
                })
            except Exception as e:
                # Log full traceback for diagnosis, but return a clean
                # error message to the frontend.
                logger.exception("Failed to run sandbox test")
                err_msg = str(e)
                # Surface the HTTP status code if present in the exception.
                status_code = getattr(e, "status_code", None)
                if status_code:
                    err_msg = f"[HTTP {status_code}] {err_msg}"
                self.send_json({
                    "ok": False,
                    "error": err_msg,
                })
            return

        # ── API: Get status ───────────────────────────────────────────
        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(_status.snapshot(), ensure_ascii=False).encode())
            return

        # ── API: Get logs ────────────────────────────────────────────
        if self.path == "/api/logs":
            self.send_json(_read_recent_logs())
            return

        # ── API: macOS WeChat automation diagnostics ─────────────────
        if self.path == "/api/macos/diagnose":
            self.send_json({
                "ok": True,
                "diagnostics": _macos_wechat_diagnostics(),
            })
            return

        # ── API: Onboarding status ────────────────────────────────────
        if self.path == "/api/onboarding/status":
            from src.config import is_onboarding_done
            done = is_onboarding_done()
            with _onboarding_lock:
                steps = {
                    "step1": _onboarding_data["step1_done"],
                    "step2": _onboarding_data["step2_done"],
                    "step3": _onboarding_data["step3_done"],
                    "step4": _onboarding_data["step4_done"],
                }
            self.send_json({"ok": True, "onboarding_done": done, "steps": steps})
            return

        # ── API: Onboarding diagnostics check ─────────────────────────
        if self.path == "/api/onboarding/diagnose":
            import sys

            # 1. Python check
            python_ok = sys.version_info >= (3, 10)
            python_val = f"Python {sys.version.split()[0]}"

            # 2. Requirements check
            req_report = _platform_dependency_report()

            # 3. WeChat PID check
            wx_report = _platform_wechat_report()

            # 4. .env check
            # In frozen mode, __file__ is inside the read-only _MEIPASS
            # extraction directory. Use PROJECT_ROOT from config.py which
            # correctly resolves to the EXE directory when frozen.
            from src.config import PROJECT_ROOT, find_env_file
            project_root = PROJECT_ROOT
            env_path = find_env_file() or (project_root / ".env")
            env_ok = env_path.exists()
            env_val = "配置文件已存在" if env_ok else "配置文件尚未创建"

            # 5. DB permissions check
            data_dir = project_root / "data"
            db_perm_ok = True
            db_perm_err = None
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                test_file = data_dir / ".write_test"
                test_file.write_text("test", encoding="utf-8")
                test_file.unlink()
            except Exception as e:
                db_perm_ok = False
                db_perm_err = str(e)

            # Check read permission to WeChat db path if it's set/detected
            db_path = None
            with _onboarding_lock:
                db_path = _onboarding_data.get("db_path")
            if not db_path:
                _, detected_db = _detect_wxid_and_db_path()
                if detected_db:
                    db_path = detected_db

            if db_path:
                db_path_obj = Path(db_path)
                if db_path_obj.exists():
                    try:
                        with open(db_path_obj, "rb") as f:
                            f.read(100)
                    except Exception as e:
                        db_perm_ok = False
                        db_perm_err = f"微信数据库读取失败: {e}"

            db_perm_val = "数据库读写权限正常" if db_perm_ok else f"数据库权限错误: {db_perm_err}"

            self.send_json({
                "ok": True,
                "diagnostics": {
                    "python": {"ok": python_ok, "value": python_val, "error": None},
                    "requirements": req_report,
                    "wechat": wx_report,
                    "env": {"ok": env_ok, "value": env_val, "error": None},
                    "db": {"ok": db_perm_ok, "value": db_perm_val, "error": db_perm_err}
                }
            })
            return

        # ── API: Onboarding step 1 - start extraction (async) ─────────
        if self.path == "/api/onboarding/step1":
            with _step1_lock:
                if _step1_state["running"]:
                    self.send_json({"ok": False, "phase": "busy", "message": "正在提取中..."})
                    return
                _step1_state["running"] = True
                _step1_state["phase"] = "idle"
                _step1_state["message"] = ""
                _step1_state["result"] = None

            # Start background thread
            t = threading.Thread(target=_run_step1_extraction, daemon=True)
            t.start()
            with _step1_lock:
                _step1_thread = t

            self.send_json({"ok": True, "phase": "started", "message": "提取已启动"})
            return

        # ── API: Onboarding step 1 - poll status ──────────────────────
        if self.path == "/api/onboarding/step1-status":
            with _step1_lock:
                s = dict(_step1_state)
            self.send_json(s)
            return

        # ── API: Onboarding step 2 - WeChat identity ──────────────────
        if self.path == "/api/onboarding/step2":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                data = json.loads(body)
                with _onboarding_lock:
                    _onboarding_data["step2_done"] = True
                    from src.config import _sanitize_display_name
                    _onboarding_data["bot_display_name"] = _sanitize_display_name(
                        data.get("bot_display_name", "群聊小助手")
                    )
                    _onboarding_data["wechat_groups"] = data.get("wechat_groups", "*")
                    _onboarding_data["wechat_backend"] = data.get("wechat_backend", "wcdb")
                    if data.get("wxid"):
                        _onboarding_data["wxid"] = data["wxid"]
                    if data.get("db_path"):
                        _onboarding_data["db_path"] = data["db_path"]
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Onboarding step 3 - AI backend ───────────────────────
        if self.path == "/api/onboarding/step3":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                data = json.loads(body)
                ai = data.get("ai_backend", "deepseek")
                with _onboarding_lock:
                    _onboarding_data["step3_done"] = True
                    _onboarding_data["ai_backend"] = ai
                    _onboarding_data["deepseek_api_key"] = data.get("deepseek_api_key", "")
                    _onboarding_data["deepseek_base_url"] = data.get("deepseek_base_url", "https://api.deepseek.com")
                    _onboarding_data["deepseek_model"] = data.get("deepseek_model", "deepseek-v4-flash")
                    _onboarding_data["anthropic_api_key"] = data.get("anthropic_api_key", "")
                    _onboarding_data["anthropic_base_url"] = data.get("anthropic_base_url", "https://api.anthropic.com")
                    _onboarding_data["summarize_model"] = data.get("summarize_model", "claude-haiku-4-5-20251001")
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Onboarding step 4 - features + write .env ────────────
        if self.path == "/api/onboarding/step4":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                data = json.loads(body)
                with _onboarding_lock:
                    _onboarding_data["step4_done"] = True
                    _onboarding_data["proactive_enabled"] = data.get("proactive_enabled", False)
                    _onboarding_data["sticky_mention_enabled"] = data.get("sticky_mention_enabled", True)

                # Write all accumulated data to .env
                env_path = _find_or_create_env()
                _write_onboarding_to_env(env_path)
                self.send_json({"ok": True})
            except Exception as e:
                logger.exception("Onboarding step4 failed")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Reset onboarding → allow re-extraction ─────────────
        if self.path == "/api/onboarding/reset":
            # 1. Reset file-based state
            env_path = _find_or_create_env()
            _set_env_key(env_path, "ONBOARDING_DONE", "false")
            _set_env_key(env_path, "WCDB_KEY", "")
            # 2. Reset in-memory state so a fresh extraction can start
            with _onboarding_lock:
                for k in _onboarding_data:
                    if isinstance(_onboarding_data[k], bool):
                        _onboarding_data[k] = False
                    elif isinstance(_onboarding_data[k], str):
                        _onboarding_data[k] = ""
            with _step1_lock:
                _step1_state["running"] = False
                _step1_state["phase"] = "idle"
                _step1_state["message"] = ""
                _step1_state["result"] = None
            self.send_json({"ok": True, "message": "请退出微信，然后点击「重新获取密钥」"})
            return

        # ── API: Browse filesystem directories ──────────────────────
        if self.path.startswith("/api/browse"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            dir_path = params.get("path", [""])[0].strip()
            if not dir_path:
                # No path given — list drives on Windows, home on others
                import platform
                if platform.system() == "Windows":
                    import string
                    drives = []
                    for letter in string.ascii_uppercase:
                        p = Path(f"{letter}:\\")
                        if p.exists():
                            drives.append({"name": f"{letter}:", "path": f"{letter}:\\", "is_dir": True})
                    self.send_json({"ok": True, "entries": drives, "current_path": ""})
                else:
                    home = Path.home()
                    entries = _list_dir_entries(home)
                    self.send_json({"ok": True, "entries": entries, "current_path": str(home)})
                return

            target = Path(dir_path)
            if not target.exists():
                self.send_json({"ok": False, "error": f"路径不存在: {dir_path}"})
                return
            if not target.is_dir():
                self.send_json({"ok": False, "error": "请选择一个目录"})
                return

            try:
                entries = _list_dir_entries(target)
                self.send_json({"ok": True, "entries": entries, "current_path": str(target)})
            except PermissionError:
                self.send_json({"ok": False, "error": "没有权限访问该目录"})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Detect WeChat data in a custom directory ──────────
        if self.path == "/api/wechat-data-dir/detect":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                data = json.loads(body)
                dir_path = (data.get("path") or "").strip()
                if not dir_path:
                    self.send_json({"ok": False, "error": "请提供目录路径"})
                    return
                target = Path(dir_path)
                if not target.exists() or not target.is_dir():
                    self.send_json({"ok": False, "error": f"目录不存在: {dir_path}"})
                    return

                # Scan for wxid_* directories
                wxid_dirs = sorted(
                    [d for d in target.iterdir() if d.is_dir() and d.name.startswith("wxid_")],
                    key=lambda d: d.stat().st_mtime, reverse=True,
                )
                accounts = []
                for wxid_dir in wxid_dirs:
                    session_db = wxid_dir / "db_storage" / "session" / "session.db"
                    accounts.append({
                        "wxid": wxid_dir.name,
                        "has_session_db": session_db.exists(),
                        "db_path": str(session_db) if session_db.exists() else "",
                    })

                if accounts:
                    self.send_json({
                        "ok": True,
                        "found": True,
                        "accounts": accounts,
                        "message": f"找到 {len(accounts)} 个微信账号",
                    })
                else:
                    self.send_json({
                        "ok": True,
                        "found": False,
                        "accounts": [],
                        "message": f"在 {dir_path} 中未找到 wxid_* 目录。请确认路径正确。",
                    })
            except Exception as e:
                logger.exception("Failed to detect WeChat data dir")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Voice model status ────────────────────────────────────
        if self.path.startswith("/api/voice/model-status"):
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                model = qs.get("model", ["small"])[0]

                # Check HuggingFace cache
                cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
                model_dir = cache_dir / f"models--Systran--faster-whisper-{model}"
                downloaded = False
                if model_dir.exists():
                    snapshots = model_dir / "snapshots"
                    if snapshots.exists() and any(snapshots.iterdir()):
                        downloaded = True
                    else:
                        blobs = model_dir / "blobs"
                        if blobs.exists() and any(blobs.iterdir()):
                            downloaded = True

                dl = _voice_downloads.get(model)
                self.send_json({
                    "ok": True,
                    "downloaded": downloaded,
                    "phase": dl.get("phase", "") if dl else "",  # "downloading" | "installing"
                    "pct": dl.get("pct", 0) if dl else 0,
                    "error": dl.get("error", "") if dl else "",
                    "model": model,
                })
            except Exception as e:
                logger.exception("Voice model-status failed")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Trigger voice model download ──────────────────────────
        if self.path == "/api/voice/download-model":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                data = json.loads(body)
                model = data.get("model", "small")

                _voice_downloads[model] = {"phase": "downloading", "pct": 0, "error": ""}

                def _run():
                    state = _voice_downloads.setdefault(model, {})
                    try:
                        # ── Phase 1: download via huggingface_hub ──
                        state["phase"] = "downloading"
                        state["pct"] = 0

                        from huggingface_hub import snapshot_download
                        cache = str(Path.home() / ".cache" / "huggingface")
                        repo_id = f"Systran/faster-whisper-{model}"

                        snapshot_download(
                            repo_id=repo_id,
                            cache_dir=cache,
                        )
                        state["pct"] = 90

                        # ── Phase 2: load model into memory ──────
                        state["phase"] = "installing"
                        state["pct"] = 95
                        from faster_whisper import WhisperModel
                        WhisperModel(
                            model, device="cpu", compute_type="int8",
                            download_root=cache,
                        )
                        state["phase"] = "done"
                        state["pct"] = 100
                        state["error"] = ""
                        logger.info("Voice model '%s' ready", model)
                    except Exception as exc:
                        state["phase"] = "error"
                        state["error"] = str(exc).split("\n")[0][:300]
                        logger.exception("Voice model download/install failed")

                t = threading.Thread(target=_run, daemon=True)
                t.start()
                self.send_json({"ok": True, "model": model, "message": "下载已在后台启动"})
            except Exception as e:
                _voice_downloads.pop(model, None)
                logger.exception("Voice download-model failed")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── SPA fallback: serve index.html for unknown paths ──────────
        if self.command != "GET" and self.command != "HEAD":
            self.send_response(405)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "Method not allowed"}).encode())
            return

        path = self.translate_path(self.path)
        if not Path(path).exists():
            self.path = "/index.html"

        super().do_GET()

    def log_message(self, format, *args):
        """Log HTTP errors but suppress normal access logs."""
        if args and any(
            code in str(args).lower()
            for code in ["error", "exception", "400", "401", "403", "404", "405", "500"]
        ):
            logger.warning("HTTP %s", format % args)

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())


def _run_server(host, port):
    """Run the HTTP server (blocking, called in daemon thread)."""
    # Enable SO_REUSEADDR so a rapid restart doesn't fail with "address in use"
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = ThreadingHTTPServer((host, port), _UIHandler)
    except OSError as e:
        logger.error("Failed to bind web server on %s:%s: %s", host, port, e)
        update_status(running=False, error=f"端口 {port} 被占用或无权绑定: {e}")
        return
    server.daemon_threads = True  # WebSocket handlers won't block exit
    logger.info("Web UI: http://%s:%s", host, port)
    try:
        server.serve_forever()
    except Exception as e:
        logger.error("Web server crashed: %s", e)


def start_web_server(host="127.0.0.1", port=7327):
    """Start the web UI in a daemon thread (idempotent)."""
    if not _server_guard.try_start():
        logger.debug("Web server already running, skipping duplicate start")
        return None

    if not UI_DIR.exists():
        logger.warning("UI not built. Run: cd ui && npm run build")
        return None

    thread = threading.Thread(
        target=_run_server, args=(host, port),
        daemon=True, name="web-ui-server",
    )
    thread.start()
    return thread
