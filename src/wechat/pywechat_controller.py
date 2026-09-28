"""Optional pywechat UI Automation sender for the WCDB receive backend.

Navigation and chat ID resolution stay with WeBot. pywechat supplies the
WeChat 4.x editor and mention-picker selectors. A failed UI check never falls
back to another sender because a draft may already exist in the composer.
"""

import logging
import time

from .window_controller import WeChatWindowController

logger = logging.getLogger(__name__)


class PyWeChatSendController(WeChatWindowController):
    """Use pywechat's WeChat 4.x UI controls after WeBot opens the chat."""

    @staticmethod
    def _select_mention(window, member: str) -> bool:
        from pyweixin.Uielements import Windows

        popover = window.child_window(**Windows.MentionPopOverWindow)
        if not popover.exists(timeout=0.5):
            return False
        mention_list = popover.child_window(control_type="List", title="")
        items = mention_list.children()
        if sum(item.window_text() == member for item in items) != 1:
            return False
        for _ in range(len(items) + 1):
            selected = [item for item in mention_list.children() if item.is_selected()]
            if selected and selected[0].window_text() == member:
                mention_list.type_keys("{ENTER}")
                return not popover.exists(timeout=0.2)
            mention_list.type_keys("{DOWN}")
        return False

    def send_message(self, hwnd: int, text: str,
                     mention: tuple[str, str] | None = None,
                     chat_id: str = "") -> bool:
        """Attempt one UIA send. True means the composer was cleared, not delivered."""
        if not text or not self._validate_hwnd(hwnd) or not self._foreground_matches(hwnd):
            return False
        try:
            import emoji
            import pyautogui
            from pywinauto import Desktop
            from pyweixin.Uielements import Edits

            window = Desktop(backend="uia").window(handle=hwnd)
            editor = window.child_window(**Edits.CurrentChatEdit)
            if not editor.exists(timeout=0.5):
                logger.warning("pywechat editor unavailable for HWND=%s", hwnd)
                return False

            body = text
            label = ""
            if mention:
                prefix, recipient_id = mention
                if not recipient_id or not text.startswith(prefix):
                    return False
                label = prefix[1:].rstrip()
                if not label or not self._mention_member_is_unique(chat_id, recipient_id, label):
                    return False
                body = text[len(prefix):]
                if not body:
                    return False
                query = emoji.replace_emoji(label, "").split(" ")[0]
                if not query:
                    return False
                editor.click_input()
                editor.type_keys("@" + query, pause=0.1)
                if not self._select_mention(window, label):
                    logger.warning("pywechat could not verify mention candidate; message unsent")
                    return False
            else:
                editor.click_input()

            if not self._foreground_matches(hwnd):
                return False
            self._set_clipboard(body)
            pyautogui.hotkey("ctrl", "v", _pause=False)
            time.sleep(self.PASTE_SEND_DELAY)
            draft = editor.window_text()
            if mention:
                valid_draft = draft.startswith("@" + label) and draft.endswith(body)
            else:
                valid_draft = draft == body
            if not valid_draft or not self._foreground_matches(hwnd):
                logger.warning("pywechat composer verification failed; message unsent")
                return False
            pyautogui.press("enter", _pause=False)
            time.sleep(self.ENTER_SEND_DELAY)
            if editor.window_text() == draft:
                logger.warning("pywechat composer did not clear after Enter")
                return False
            return True
        except ImportError:
            logger.exception("pywechat send engine is not installed")
            return False
        except Exception:
            logger.exception("pywechat UI send failed")
            return False
