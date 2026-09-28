"""The optional pywechat sender must fail before pressing Enter on UI mismatch."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.wechat.pywechat_controller import PyWeChatSendController
from src.wechat.wcdb_backend import WcdbBackend


def test_wcdb_receiving_backend_selects_optional_sender():
    backend = WcdbBackend(config=SimpleNamespace(wechat_backend="wcdb_pywechat"))
    assert isinstance(backend._window, PyWeChatSendController)


def _sender_with_editor(draft_values):
    sender = PyWeChatSendController()
    sender._validate_hwnd = MagicMock(return_value=True)
    sender._foreground_matches = MagicMock(return_value=True)
    sender._set_clipboard = MagicMock()
    editor = MagicMock()
    editor.exists.return_value = True
    editor.window_text.side_effect = draft_values
    window = MagicMock()
    window.child_window.return_value = editor
    return sender, editor, window


def test_plain_message_sends_only_after_draft_matches():
    sender, editor, window = _sender_with_editor(["hello", ""])
    with patch("pywinauto.Desktop") as desktop, patch("pyautogui.hotkey") as paste, \
            patch("pyautogui.press") as enter, patch("src.wechat.pywechat_controller.time.sleep"):
        desktop.return_value.window.return_value = window
        assert sender.send_message(123, "hello") is True
    sender._set_clipboard.assert_called_once_with("hello")
    paste.assert_called_once_with("ctrl", "v", _pause=False)
    enter.assert_called_once_with("enter", _pause=False)


def test_mismatched_draft_never_presses_enter():
    sender, editor, window = _sender_with_editor(["previous draft hello"])
    with patch("pywinauto.Desktop") as desktop, patch("pyautogui.hotkey"), \
            patch("pyautogui.press") as enter, patch("src.wechat.pywechat_controller.time.sleep"):
        desktop.return_value.window.return_value = window
        assert sender.send_message(123, "hello") is False
    enter.assert_not_called()


def test_mention_uses_full_name_candidate_and_appends_body():
    sender, editor, window = _sender_with_editor(["@Alice Smith hello", ""])
    sender._mention_member_is_unique = MagicMock(return_value=True)
    sender._select_mention = MagicMock(return_value=True)
    with patch("pywinauto.Desktop") as desktop, patch("pyautogui.hotkey"), \
            patch("pyautogui.press") as enter, patch("src.wechat.pywechat_controller.time.sleep"):
        desktop.return_value.window.return_value = window
        assert sender.send_message(
            123, "@Alice Smith hello", mention=("@Alice Smith ", "wxid_alice"),
            chat_id="test@chatroom",
        ) is True
    editor.type_keys.assert_called_once_with("@Alice", pause=0.1)
    sender._select_mention.assert_called_once_with(window, "Alice Smith")
    sender._set_clipboard.assert_called_once_with("hello")
    enter.assert_called_once_with("enter", _pause=False)


def test_missing_ui_editor_does_not_send():
    sender, editor, window = _sender_with_editor([])
    editor.exists.return_value = False
    with patch("pywinauto.Desktop") as desktop, patch("pyautogui.press") as enter:
        desktop.return_value.window.return_value = window
        assert sender.send_message(123, "hello") is False
    enter.assert_not_called()
