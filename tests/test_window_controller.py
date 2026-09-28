import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

if sys.platform != "win32":
    pytest.skip("Windows-only HWND controller tests", allow_module_level=True)

from src.wechat.window_controller import WeChatWindowController, WindowCandidate


class WeChatWindowControllerTests(unittest.TestCase):
    def test_exact_group_title_identifies_detached_chat(self):
        controller = WeChatWindowController()
        candidates = {
            100: WindowCandidate(hwnd=100, title="微信", process_name="weixin.exe",
                                 rect=(0, 0, 1100, 700), visible=True),
            200: WindowCandidate(hwnd=200, title="target group", process_name="weixin.exe",
                                 rect=(0, 0, 600, 600), visible=True),
        }

        def enum_windows(callback, context):
            for hwnd in candidates:
                callback(hwnd, context)

        with (
            patch("src.wechat.window_controller.win32gui.EnumWindows", side_effect=enum_windows),
            patch("src.wechat.window_controller._score_window", side_effect=lambda hwnd: candidates[hwnd]),
        ):
            self.assertEqual(controller._find_target_chat_window("target group"), 200)
            self.assertIsNone(controller._find_target_chat_window("another group"))

    def test_send_uses_existing_detached_chat_without_reopening_search(self):
        controller = WeChatWindowController()
        with (
            patch.object(controller, "_find_target_chat_window", return_value=200),
            patch.object(controller, "find_hwnd") as find_main,
            patch.object(controller, "activate", return_value=True),
            patch.object(controller, "_looks_like_blank_window", return_value=False),
            patch.object(controller, "navigate_to_chat") as navigate,
            patch.object(controller, "send_message", return_value=True) as send,
        ):
            self.assertTrue(controller.send_to_chat("target group", "hello", max_retries=1))
            find_main.assert_not_called()
            navigate.assert_not_called()
            send.assert_called_once_with(200, "hello")

    def test_navigate_to_chat_uses_keyboard_only(self):
        """Navigation should use only keyboard (Ctrl+F, paste, Enter, Tab),
        no mouse clicks."""
        controller = WeChatWindowController()

        with (
            patch.object(controller, "_validate_hwnd", return_value=True),
            patch.object(controller, "_foreground_matches", return_value=True),
            patch("src.wechat.window_controller.press_key") as press_key,
            patch("src.wechat.window_controller.send_combo") as send_combo,
            patch.object(controller, "_set_clipboard"),
            patch.object(controller, "_verify_chat_title", return_value=False),
            patch.object(controller, "_current_wechat_foreground_hwnd", return_value=None),
            patch.object(
                controller,
                "get_foreground_info",
                return_value="FG_HWND=12345 title='微信' class='Qt51514QWindowIcon' proc=wechat.exe",
            ),
        ):
            self.assertTrue(controller.navigate_to_chat(12345, "target group"))

            # Verify keyboard keys were pressed (not mouse clicks)
            pressed_keys = [
                call[0][0] for call in press_key.call_args_list
            ]
            # Only Enter (no Esc, no Tab)
            self.assertIn(0x0D, pressed_keys)  # Enter
            self.assertNotIn(0x1B, pressed_keys)  # Esc must NOT be pressed
            self.assertNotIn(0x09, pressed_keys)  # Tab must NOT be pressed

            # Verify Ctrl+F and Ctrl+V combos
            combo_keys = [
                (call[0][0], call[0][1]) for call in send_combo.call_args_list
            ]
            self.assertIn((0x11, 0x46), combo_keys)  # Ctrl+F
            self.assertIn((0x11, 0x56), combo_keys)  # Ctrl+V

    def test_navigate_to_chat_fails_when_wechat_is_not_foreground(self):
        controller = WeChatWindowController()

        with (
            patch.object(controller, "_validate_hwnd", return_value=True),
            patch.object(controller, "_foreground_matches", return_value=False),
            patch("src.wechat.window_controller.press_key"),
            patch("src.wechat.window_controller.send_combo"),
            patch.object(controller, "_set_clipboard"),
            patch.object(controller, "_verify_chat_title", return_value=False),
            patch.object(
                controller,
                "get_foreground_info",
                return_value="FG_HWND=1 title='Explorer' class='CabinetWClass' proc=explorer.exe",
            ),
        ):
            self.assertFalse(controller.navigate_to_chat(12345, "target group"))

    def test_send_message_fails_when_wechat_is_not_foreground(self):
        controller = WeChatWindowController()

        with (
            patch.object(controller, "_validate_hwnd", return_value=True),
            patch.object(controller, "_foreground_matches", return_value=False),
            patch.object(controller, "_set_clipboard") as set_clipboard,
            patch("src.wechat.window_controller.send_combo") as send_combo,
            patch("src.wechat.window_controller.press_key") as press_key,
        ):
            self.assertFalse(controller.send_message(12345, "hello"))
            set_clipboard.assert_not_called()
            send_combo.assert_not_called()
            press_key.assert_not_called()

    def test_send_message_uses_keyboard_only(self):
        """Send should use Ctrl+V paste + Enter, no mouse clicks."""
        controller = WeChatWindowController()

        with (
            patch.object(controller, "_validate_hwnd", return_value=True),
            patch.object(controller, "_foreground_matches", return_value=True),
            patch.object(controller, "_set_clipboard"),
            patch("src.wechat.window_controller.send_combo") as send_combo,
            patch("src.wechat.window_controller.press_key") as press_key,
            patch.object(controller, "_current_wechat_foreground_hwnd", return_value=None),
        ):
            self.assertTrue(controller.send_message(12345, "hello"))

            # Should use Ctrl+V for paste
            send_combo.assert_called_once_with(0x11, 0x56)

            # Should use Enter to send (not click send button)
            enter_calls = [
                c for c in press_key.call_args_list if c[0][0] == 0x0D
            ]
            self.assertTrue(len(enter_calls) > 0, "Enter key should be pressed to send")

    def test_native_mention_requires_verified_picker_selection(self):
        controller = WeChatWindowController()
        with (
            patch.object(controller, "_validate_hwnd", return_value=True),
            patch.object(controller, "_foreground_matches", return_value=True),
            patch.object(controller, "_mention_window_handles", return_value={12345}),
            patch.object(controller, "_mention_member_is_unique", return_value=True),
            patch.object(controller, "_select_mention_candidate", return_value=False),
            patch("src.wechat.window_controller.type_unicode"),
            patch.object(controller, "_set_clipboard") as clipboard,
            patch("src.wechat.window_controller.send_combo"),
            patch("src.wechat.window_controller.press_key") as press_key,
            patch("src.wechat.window_controller.time.sleep"),
        ):
            self.assertFalse(controller.send_message(
                12345, "@Alice 你好", mention=("@Alice ", "wxid_alice")))
            self.assertNotIn(0x0D, [call.args[0] for call in press_key.call_args_list])
            clipboard.assert_not_called()

    def test_native_mention_sends_body_only_after_picker_selection(self):
        controller = WeChatWindowController()
        clipboard = []
        with (
            patch.object(controller, "_validate_hwnd", return_value=True),
            patch.object(controller, "_foreground_matches", return_value=True),
            patch.object(controller, "_mention_window_handles", return_value={12345}),
            patch.object(controller, "_mention_member_is_unique", return_value=True),
            patch.object(controller, "_select_mention_candidate", return_value=True) as picker,
            patch("src.wechat.window_controller.type_unicode") as typed,
            patch.object(controller, "_set_clipboard", side_effect=clipboard.append),
            patch("src.wechat.window_controller.send_combo") as send_combo,
            patch("src.wechat.window_controller.press_key") as press_key,
            patch("src.wechat.window_controller.time.sleep"),
        ):
            self.assertTrue(controller.send_message(
                12345, "@Alice 你好", mention=("@Alice ", "wxid_alice")))
            picker.assert_called_once_with(12345, {12345})
            typed.assert_called_once_with("Alice")
            self.assertEqual(clipboard, ["你好"])
            self.assertIn((0x10, 0x32), [call.args for call in send_combo.call_args_list])
            self.assertIn(0x0D, [call.args[0] for call in press_key.call_args_list])

    def test_native_mention_rejects_spaces_before_opening_picker(self):
        controller = WeChatWindowController()
        with (
            patch.object(controller, "_validate_hwnd", return_value=True),
            patch.object(controller, "_foreground_matches", return_value=True),
            patch.object(controller, "_mention_window_handles") as windows,
            patch("src.wechat.window_controller.send_combo") as combo,
            patch("src.wechat.window_controller.press_key") as key,
        ):
            self.assertFalse(controller.send_message(
                12345, "@Alice Smith 你好", mention=("@Alice Smith ", "wxid_alice")))
            windows.assert_not_called()
            combo.assert_not_called()
            key.assert_not_called()

    def test_native_mention_requires_unique_member_id(self):
        controller = WeChatWindowController()
        members = '{"group": {"id1": "Alice", "id2": "Alice"}}'
        with patch("src.wechat.window_controller.Path.read_text", return_value=members):
            self.assertFalse(controller._mention_member_is_unique("group", "id1", "Alice"))
        members = '{"group": {"id1": "Alice", "id2": "Bob"}}'
        with patch("src.wechat.window_controller.Path.read_text", return_value=members):
            self.assertTrue(controller._mention_member_is_unique("group", "id1", "Alice"))
            self.assertFalse(controller._mention_member_is_unique("other", "id1", "Alice"))

    def test_native_mention_picker_click_requires_popup_to_close(self):
        def enumerate_windows(callback, output):
            callback(200, output)

        with (
            patch("src.wechat.window_controller.win32process.GetWindowThreadProcessId",
                  return_value=(1, 10)),
            patch("src.wechat.window_controller.win32gui.EnumWindows",
                  side_effect=enumerate_windows),
            patch("src.wechat.window_controller.win32gui.IsWindowVisible",
                  side_effect=[True, False]),
            patch("src.wechat.window_controller.win32gui.GetClassName",
                  return_value="Qt51514QWindowToolSaveBits"),
            patch("src.wechat.window_controller.win32gui.GetWindowRect",
                  return_value=(0, 0, 200, 50)),
            patch("src.wechat.window_controller.win32api.GetCursorPos", return_value=(300, 300)),
            patch("src.wechat.window_controller.win32api.SetCursorPos") as cursor,
            patch("src.wechat.window_controller.win32api.mouse_event") as click,
            patch.object(WeChatWindowController, "_foreground_matches", return_value=True),
            patch("src.wechat.window_controller.time.sleep"),
        ):
            self.assertTrue(WeChatWindowController._select_mention_candidate(100, {100}))
            self.assertEqual(click.call_count, 2)
            self.assertEqual(cursor.call_args.args, ((300, 300),))

    def test_native_mention_picker_rejects_multi_row_popup(self):
        def enumerate_windows(callback, output):
            callback(200, output)

        with (
            patch("src.wechat.window_controller.win32process.GetWindowThreadProcessId",
                  return_value=(1, 10)),
            patch("src.wechat.window_controller.win32gui.EnumWindows",
                  side_effect=enumerate_windows),
            patch("src.wechat.window_controller.win32gui.IsWindowVisible", return_value=True),
            patch("src.wechat.window_controller.win32gui.GetClassName",
                  return_value="Qt51514QWindowToolSaveBits"),
            patch("src.wechat.window_controller.win32gui.GetWindowRect",
                  return_value=(0, 0, 200, 160)),
            patch("src.wechat.window_controller.win32api.mouse_event") as click,
        ):
            self.assertFalse(WeChatWindowController._select_mention_candidate(100, {100}))
            click.assert_not_called()

    def test_find_hwnd_rejects_small_wechat_login_prompt(self):
        controller = WeChatWindowController()
        small_prompt = WindowCandidate(
            hwnd=200,
            title="微信",
            class_name="Qt51514QWindowIcon",
            pid=1,
            process_name="weixin.exe",
            rect=(0, 0, 180, 150),   # below 200×200 minimum
            visible=True,
            iconic=False,
            score=150,
            reason="wechat_process+qt_class+visible+too_small(180x150)",
        )

        def enum_windows(callback, ctx):
            callback(200, ctx)

        with (
            patch("src.wechat.window_controller.win32gui.EnumWindows", side_effect=enum_windows),
            patch("src.wechat.window_controller._score_window", return_value=small_prompt),
        ):
            self.assertIsNone(controller.find_hwnd(force=True))

    def test_send_to_chat_adopts_new_wechat_foreground_hwnd_after_navigation(self):
        controller = WeChatWindowController()
        states = {"hwnd": 100}

        def navigate(hwnd, _group_name):
            states["hwnd"] = 200
            return 200

        def send_message(hwnd, _text):
            self.assertEqual(hwnd, 200)
            return True

        with (
            patch.object(controller, "find_hwnd", return_value=100),
            patch.object(controller, "activate", return_value=True),
            patch.object(controller, "navigate_to_chat", side_effect=navigate),
            patch.object(controller, "send_message", side_effect=send_message),
            patch.object(controller, "_current_wechat_foreground_hwnd", side_effect=lambda: states["hwnd"]),
            patch.object(controller, "_log_failure"),
        ):
            self.assertTrue(controller.send_to_chat("target group", "hello", max_retries=1))

    def test_send_to_chat_refuses_blank_wechat_window(self):
        controller = WeChatWindowController()

        with (
            patch.object(controller, "find_hwnd", return_value=100),
            patch.object(controller, "activate", return_value=True),
            patch.object(controller, "_looks_like_blank_window", return_value=True),
            patch.object(controller, "navigate_to_chat") as navigate,
            patch.object(controller, "send_message") as send_message,
            patch.object(controller, "_log_failure") as log_failure,
        ):
            self.assertFalse(controller.send_to_chat("target group", "hello", max_retries=1))
            navigate.assert_not_called()
            send_message.assert_not_called()
            log_failure.assert_called_with(
                "target group", "hello", "WeChat window is blank/white", 100
            )

    def test_send_failure_after_navigation_does_not_repeat_possible_paste(self):
        controller = WeChatWindowController()
        with (
            patch.object(controller, "_find_target_chat_window", return_value=200),
            patch.object(controller, "activate", return_value=True),
            patch.object(controller, "_looks_like_blank_window", return_value=False),
            patch.object(controller, "send_message", return_value=False) as send,
            patch.object(controller, "_log_failure"),
        ):
            self.assertFalse(controller.send_to_chat("target group", "hello", max_retries=3))
            send.assert_called_once_with(200, "hello")



if __name__ == "__main__":
    unittest.main()
