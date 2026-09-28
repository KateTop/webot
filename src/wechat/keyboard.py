"""Shared keyboard simulation helpers via Win32 keybd_event.

These are used by window_controller to inject keystrokes into the
foreground window.
"""

import ctypes
import time
from ctypes import wintypes


def press_key(vk: int) -> None:
    """Send one key press/release to the foreground window."""
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


def send_combo(mod_vk: int, key_vk: int) -> None:
    """Send a modifier+key combo (e.g. Ctrl+F) to the foreground window."""
    ctypes.windll.user32.keybd_event(mod_vk, 0, 0, 0)
    time.sleep(0.03)
    ctypes.windll.user32.keybd_event(key_vk, 0, 0, 0)
    time.sleep(0.03)
    ctypes.windll.user32.keybd_event(key_vk, 0, 2, 0)
    time.sleep(0.03)
    ctypes.windll.user32.keybd_event(mod_vk, 0, 2, 0)


def type_unicode(text: str, delay: float = 0.06) -> None:
    """Type UTF-16 characters as keystrokes without pasting into a Qt picker."""
    class KeybdInput(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class InputUnion(ctypes.Union):
        _fields_ = [("ki", KeybdInput), ("padding", ctypes.c_byte * 32)]

    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]

    encoded = text.encode("utf-16-le")
    for offset in range(0, len(encoded), 2):
        scan = int.from_bytes(encoded[offset:offset + 2], "little")
        for flags in (0x0004, 0x0004 | 0x0002):  # KEYEVENTF_UNICODE / KEYUP
            event = Input(1, InputUnion(ki=KeybdInput(0, scan, flags, 0, 0)))
            if ctypes.windll.user32.SendInput(1, ctypes.byref(event),
                                             ctypes.sizeof(event)) != 1:
                raise OSError("Unicode keyboard input failed")
        time.sleep(delay)
