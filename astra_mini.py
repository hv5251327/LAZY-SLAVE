# ===============================================
#  ASTRA MINI v3.1 - ROLEX - Voice-Controlled Screen Agent (FIXED)
# ===============================================
#
#  What was broken in the original and is fixed here:
#
#  1. DUPLICATE Win32 SENDINPUT BLOCK (the big one)
#     The script defined MOUSEINPUT/INPUT_UNION/INPUT, reliable_click etc.
#     TWICE. The second (mouse-only) block re-bound INPUT_UNION, so when
#     _send_key() later did INPUT_UNION(ki=KEYBDINPUT(...)) it raised
#     TypeError. Result: ALL volume keys (vol up/down/mute), media keys
#     (play/pause/next/prev) silently died inside the main try/except.
#     -> Fixed: ONE clean SendInput definition for mouse AND keyboard.
#
#  2. "move" COMMANDS NEVER MATCHED LOCALLY
#     "move mouse to the right", "move the cursor left", "slide pointer
#     down" etc. have words BETWEEN the verb and the direction, so the
#     regex `move\s+(?:to\s+)?(?:the\s+)?(right|left|...)` never matched,
#     and the command fell through to Ollama (15-20s, usually "unknown").
#     -> Fixed: normalization collapses verb + mouse/cursor/pointer +
#        optional to/towards into "move <direction>", and the move regex
#        itself also tolerates the intervening words.
#
#  3. POLITE FILLERS WENT TO THE LLM
#     "can you open chrome please", "please scroll down" did not match
#     local rules -> Ollama translation -> seconds of latency.
#     -> Fixed: leading ("please","can you","could you",...) and trailing
#        ("please","for me","now") fillers are stripped up front.
#
#  4. QUESTIONS WERE SENT TO OLLAMA (expensive!)
#     "what time is it", "how are you" etc. hit the 15-20s Ollama call.
#     -> Fixed: question-words short-circuit to action="unknown" before
#        the Ollama fallback. Ollama is now a true last resort only.
#
#  5. MORE LOCAL RULES SO THE LLM IS (ALMOST) NEVER NEEDED
#     "go to <url/site>", "turn up/down the volume", "raise the volume",
#     "shut down chrome" -> close chrome, "select all" stays intact,
#     "select <word>" reaches the real Select action again (it was being
#     swallowed by the click synonyms).
#
#  6. SLOW PER-TURN RESPONSE
#     - Silence windows trimmed (0.4/1.0s -> 0.35/0.8s)
#     - wait_for_tts() now waits for ACTUAL speech end (not queue drain),
#       which also stops Rolex hearing its own voice through the mic.
#     - Ollama timeouts reduced (translate 15->8s, planner 30->20s) and
#       the translator prompt shrunk so even the fallback is faster.
#     - OCR/vision take ONE screenshot instead of two.
#
#  7. Startup is quicker/quieter (single Win32 init, no double definitions,
#     DEBUG_MODE off, mixed unused code removed).
#
#  NOTES:
#  - Ollama is OPTIONAL. If it is offline, every local command still works
#    and nothing slows down. Set ENABLE_LLM = False to ban the LLM entirely.
#  - Needs: pyautogui SpeechRecognition sounddevice pyttsx3 Pillow numpy
#           pywin32 pythoncom, winocr (Windows), psutil, redis (optional)
#  - Ollama models (optional):  ollama pull qwen2.5:1.5b
#                               ollama pull moondream:1.8b
# ===============================================

import os
import sys
import time
import json
import subprocess
import threading
import io
import wave
import asyncio
import re
import base64
import urllib.parse
from difflib import SequenceMatcher
from functools import lru_cache

import numpy as np
import sounddevice as sd
import pyttsx3
import pyautogui
from PIL import Image

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Set False to forbid ANY call to the local LLM (Ollama).
ENABLE_LLM = True

DEBUG_MODE = False

def debug_log(msg):
    if DEBUG_MODE:
        print(f"  [DEBUG] {msg}")


# =============================================================
#  Win32 API - ONE definition, 64-bit safe
# =============================================================
try:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _shcore = None
    try:
        _shcore = ctypes.windll.shcore
    except Exception:
        pass

    # ---- SendInput structures (defined FIRST, then signatures) ----
    PUL = ctypes.POINTER(ctypes.c_ulong)

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", PUL)]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("_u",)
        _fields_ = [("type", ctypes.c_ulong), ("_u", INPUT_UNION)]

    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.SetCursorPos.restype = wintypes.BOOL
    _user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.SendInput.restype = ctypes.c_uint
    _user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
    _user32.mouse_event.restype = None
    _user32.mouse_event.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_ulong, ctypes.c_void_p]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    # DPI awareness
    try:
        if _shcore:
            _shcore.SetProcessDpiAwareness.restype = ctypes.c_long
            _shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
            _shcore.SetProcessDpiAwareness(2)
            print("  [*] DPI awareness: per-monitor")
        else:
            raise Exception("no shcore")
    except Exception:
        try:
            _user32.SetProcessDPIAware.restype = wintypes.BOOL
            _user32.SetProcessDPIAware.argtypes = []
            _user32.SetProcessDPIAware()
            print("  [*] DPI awareness: system")
        except Exception as _e:
            print(f"  [!] DPI awareness failed: {_e}")

    # Constants
    INPUT_MOUSE           = 0
    INPUT_KEYBOARD        = 1
    MOUSEEVENTF_LEFTDOWN  = 0x0002
    MOUSEEVENTF_LEFTUP    = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP   = 0x0010
    MOUSEEVENTF_WHEEL     = 0x0800
    KEYEVENTF_KEYUP       = 0x0002
    KEYEVENTF_EXTENDEDKEY = 0x0001
    WHEEL_DELTA           = 120

    VK_VOLUME_MUTE = 0xAD
    VK_VOLUME_DOWN = 0xAE
    VK_VOLUME_UP   = 0xAF
    VK_MEDIA_PLAY  = 0xB3
    VK_MEDIA_NEXT  = 0xB0
    VK_MEDIA_PREV  = 0xB1

    # Media / navigation keys that need the EXTENDED flag
    _EXTENDED_VKS = {VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP,
                     VK_MEDIA_PLAY, VK_MEDIA_NEXT, VK_MEDIA_PREV,
                     0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}

    _WIN32_OK = True
    print("  [*] Win32 API initialized (64-bit safe)")

    def _send_click():
        down = INPUT(type=INPUT_MOUSE, _u=INPUT_UNION(mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_LEFTDOWN)))
        up   = INPUT(type=INPUT_MOUSE, _u=INPUT_UNION(mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_LEFTUP)))
        _user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        time.sleep(0.08)
        _user32.SendInput(1, ctypes.byref(up),   ctypes.sizeof(INPUT))

    def reliable_click(x, y):
        x, y = int(x), int(y)
        _user32.SetCursorPos(x, y)
        time.sleep(0.04)
        _send_click()
        print(f"  [Click] ({x}, {y})")

    def reliable_double_click(x, y):
        x, y = int(x), int(y)
        _user32.SetCursorPos(x, y)
        time.sleep(0.04)
        _send_click()
        time.sleep(0.05)
        _send_click()
        print(f"  [DblClick] ({x}, {y})")

    def right_click_at(x, y):
        x, y = int(x), int(y)
        _user32.SetCursorPos(x, y)
        time.sleep(0.04)
        down = INPUT(type=INPUT_MOUSE, _u=INPUT_UNION(mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_RIGHTDOWN)))
        up   = INPUT(type=INPUT_MOUSE, _u=INPUT_UNION(mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_RIGHTUP)))
        _user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        time.sleep(0.08)
        _user32.SendInput(1, ctypes.byref(up),   ctypes.sizeof(INPUT))

    def _send_key(vk, key_up=False):
        flags = 0
        if vk in _EXTENDED_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        if key_up:
            flags |= KEYEVENTF_KEYUP
        inp = INPUT(type=INPUT_KEYBOARD,
                    _u=INPUT_UNION(ki=KEYBDINPUT(wVk=vk, dwFlags=flags)))
        _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def press_vk(vk, times=1, delay=0.08):
        """Press a virtual-key N times via SendInput (reliable for media keys)."""
        for _ in range(max(1, int(times))):
            _send_key(vk, key_up=False)
            time.sleep(0.03)
            _send_key(vk, key_up=True)
            time.sleep(delay)

    def hw_scroll(clicks):
        """Scroll via win32 mouse_event. Positive = up, negative = down."""
        try:
            _user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(clicks) * WHEEL_DELTA, 0)
        except Exception:
            pyautogui.scroll(int(clicks) * 100)

    def _cursor_pos():
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

except Exception as _e:
    print(f"  [!] win32 init failed ({_e}); using pyautogui fallback")
    _WIN32_OK = False
    VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
    VK_MEDIA_PLAY, VK_MEDIA_NEXT, VK_MEDIA_PREV = 0xB3, 0xB0, 0xB1
    MOUSEEVENTF_WHEEL = 0x0800

    def reliable_click(x, y):
        pyautogui.click(x=int(x), y=int(y))

    def reliable_double_click(x, y):
        pyautogui.doubleClick(x=int(x), y=int(y))

    def right_click_at(x, y):
        pyautogui.click(x=int(x), y=int(y), button="right")

    def press_vk(vk, times=1, delay=0.08):
        key_map = {VK_VOLUME_MUTE: "volumemute", VK_VOLUME_DOWN: "volumedown",
                   VK_VOLUME_UP: "volumeup", VK_MEDIA_PLAY: "playpause",
                   VK_MEDIA_NEXT: "nexttrack", VK_MEDIA_PREV: "prevtrack"}
        key = key_map.get(vk)
        for _ in range(max(1, int(times))):
            if key:
                pyautogui.press(key)
            time.sleep(delay)

    def hw_scroll(clicks):
        pyautogui.scroll(int(clicks) * 100)

    def _cursor_pos():
        try:
            import pyautogui as _pg
            return _pg.position()
        except Exception:
            return (0, 0)


# =============================================================
#  Window enumeration & management
# =============================================================
import ctypes.wintypes as wintypes

try:
    EnumWindows = _user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    GetWindowText = _user32.GetWindowTextW
    GetWindowTextLength = _user32.GetWindowTextLengthW
    IsWindowVisible = _user32.IsWindowVisible
    IsIconic = _user32.IsIconic
    GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
    ShowWindow = _user32.ShowWindow
    PostMessage = _user32.PostMessageW
    SetForegroundWindow = _user32.SetForegroundWindow
    _ENUM_OK = True
except Exception:
    _ENUM_OK = False

SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE  = 9
SW_SHOW     = 5
WM_CLOSE    = 0x0010


def get_process_name(hwnd):
    try:
        import psutil
        pid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return psutil.Process(pid.value).name().lower()
    except Exception:
        return ""


def enum_windows():
    results = []
    if not _ENUM_OK:
        return results

    def _cb(hwnd, lparam):
        if not IsWindowVisible(hwnd):
            return True
        length = GetWindowTextLength(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        GetWindowText(hwnd, buf, length + 1)
        title = buf.value
        if title in ("Program Manager", "Setting", ""):
            return True
        results.append({
            "hwnd": hwnd,
            "title": title,
            "process": get_process_name(hwnd),
            "minimized": bool(IsIconic(hwnd)),
        })
        return True

    EnumWindows(EnumWindowsProc(_cb), 0)
    return results


def find_windows_by_name(name):
    name = name.lower().strip()
    wins = enum_windows()
    matches = []
    proc_aliases = {
        "chrome": ["chrome.exe"], "edge": ["msedge.exe"],
        "firefox": ["firefox.exe"], "brave": ["brave.exe"],
        "vlc": ["vlc.exe"], "notepad": ["notepad.exe"],
        "explorer": ["explorer.exe"], "file explorer": ["explorer.exe"],
        "calculator": ["calculatorapp.exe", "calc.exe"],
        "spotify": ["spotify.exe"], "discord": ["discord.exe"],
        "word": ["winword.exe"], "excel": ["excel.exe"],
    }
    target_procs = proc_aliases.get(name, [name + ".exe", name])
    for w in wins:
        if any(tp in w["process"] for tp in target_procs):
            matches.append(w)
        elif name in w["title"].lower():
            matches.append(w)
    return matches


def close_window_by_name(name, occurrence=1):
    matches = find_windows_by_name(name)
    if not matches:
        speak(f"No {name} windows found")
        return
    if str(occurrence).lower() == "all":
        speak(f"Closing all {len(matches)} {name} windows")
        for w in matches:
            PostMessage(w["hwnd"], WM_CLOSE, 0, 0)
            time.sleep(0.1)
        return
    try:
        n = int(occurrence)
    except Exception:
        n = 1
    if n > len(matches):
        speak(f"Only {len(matches)} {name} windows open")
        return
    speak(f"Closing {name} window {n}")
    PostMessage(matches[n - 1]["hwnd"], WM_CLOSE, 0, 0)


def minimize_window_by_name(name, occurrence=1):
    matches = find_windows_by_name(name)
    if not matches:
        speak(f"No {name} windows found")
        return
    if str(occurrence).lower() == "all":
        speak(f"Minimizing all {name} windows")
        for w in matches:
            ShowWindow(w["hwnd"], SW_MINIMIZE)
        return
    try:
        n = int(occurrence)
    except Exception:
        n = 1
    if n > len(matches):
        speak(f"Only {len(matches)} {name} windows")
        return
    speak(f"Minimizing {name} window {n}")
    ShowWindow(matches[n - 1]["hwnd"], SW_MINIMIZE)


def restore_window_by_name(name, occurrence=1):
    matches = find_windows_by_name(name)
    if not matches:
        speak(f"No {name} windows")
        return
    minimized = [w for w in matches if w["minimized"]]
    pool = minimized if minimized else matches
    try:
        n = int(occurrence)
    except Exception:
        n = 1
    if n > len(pool):
        n = 1
    target = pool[n - 1]
    speak(f"Restoring {name}")
    ShowWindow(target["hwnd"], SW_RESTORE)
    SetForegroundWindow(target["hwnd"])


def maximize_window_by_name(name, occurrence=1):
    matches = find_windows_by_name(name)
    if not matches:
        speak(f"No {name} windows found")
        return
    if str(occurrence).lower() == "all":
        speak(f"Maximizing all {len(matches)} {name} windows")
        for w in matches:
            if w["minimized"]:
                ShowWindow(w["hwnd"], SW_RESTORE)
            ShowWindow(w["hwnd"], SW_MAXIMIZE)
            time.sleep(0.1)
        return
    try:
        n = int(occurrence)
    except Exception:
        n = 1
    if n > len(matches):
        speak(f"Only {len(matches)} {name} windows")
        return
    target = matches[n - 1]
    speak(f"Maximizing {name}")
    if target["minimized"]:
        ShowWindow(target["hwnd"], SW_RESTORE)
    ShowWindow(target["hwnd"], SW_MAXIMIZE)
    SetForegroundWindow(target["hwnd"])


def close_all_minimized():
    wins = enum_windows()
    minimized = [w for w in wins if w["minimized"]]
    if not minimized:
        speak("No minimized windows")
        return
    speak(f"Closing {len(minimized)} minimized windows")
    for w in minimized:
        PostMessage(w["hwnd"], WM_CLOSE, 0, 0)
        time.sleep(0.08)


def minimize_current():
    try:
        hwnd = _user32.GetForegroundWindow()
        _user32.ShowWindow(hwnd, SW_MINIMIZE)
    except Exception:
        pyautogui.hotkey("win", "down")


def maximize_current():
    speak("Maximizing")
    try:
        _user32.ShowWindow(_user32.GetForegroundWindow(), SW_MAXIMIZE)
    except Exception:
        pyautogui.hotkey("win", "up")


def restore_current():
    speak("Restoring window")
    try:
        _user32.ShowWindow(_user32.GetForegroundWindow(), SW_RESTORE)
    except Exception:
        pyautogui.hotkey("win", "down")


def maximize_all_windows():
    wins = enum_windows()
    if not wins:
        speak("No windows open")
        return
    speak(f"Maximizing all {len(wins)} windows")
    for w in wins:
        try:
            if w["minimized"]:
                ShowWindow(w["hwnd"], SW_RESTORE)
            ShowWindow(w["hwnd"], SW_MAXIMIZE)
            time.sleep(0.05)
        except Exception:
            pass


def restore_all_minimized():
    wins = enum_windows()
    minimized = [w for w in wins if w["minimized"]]
    if not minimized:
        speak("No minimized windows")
        return
    speak(f"Restoring {len(minimized)} minimized windows")
    for w in minimized:
        ShowWindow(w["hwnd"], SW_RESTORE)
        time.sleep(0.05)


def minimize_all_windows():
    speak("Minimizing all windows")
    pyautogui.hotkey("win", "d")


def restore_all_windows():
    speak("Restoring windows")
    pyautogui.hotkey("win", "shift", "m")


def list_open_windows():
    wins = enum_windows()
    if not wins:
        speak("No windows open")
        return
    print("\n  === Open Windows ===")
    for i, w in enumerate(wins, 1):
        state = "[MIN]" if w["minimized"] else "[   ]"
        print(f"  {i}. {state} {w['process']:20s} | {w['title'][:60]}")
    print()
    speak(f"{len(wins)} windows open. Check console.")


def click_taskbar_icon(name, occurrence=1):
    matches = find_windows_by_name(name)
    if not matches:
        speak(f"No {name} in taskbar")
        return
    try:
        n = int(occurrence)
    except Exception:
        n = 1
    if n > len(matches):
        n = 1
    target = matches[n - 1]
    speak(f"Switching to {name}")
    if target["minimized"]:
        ShowWindow(target["hwnd"], SW_RESTORE)
    SetForegroundWindow(target["hwnd"])


# ---- Detect windows that need double-click (File Explorer, Desktop) ----
def get_foreground_class():
    try:
        hwnd = _user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buf, 256)
        return buf.value or ""
    except Exception:
        return ""


def needs_double_click():
    cls = get_foreground_class().lower()
    return any(k in cls for k in ("cabinetwclass", "progman", "workerw", "explorewclass"))


def smart_click(x, y):
    if needs_double_click():
        print("  [Smart] File Explorer / Desktop detected -> double click")
        reliable_double_click(x, y)
    else:
        reliable_click(x, y)


# =============================================================
#  Mouse movement (relative)
# =============================================================
_DIR_H = {"right": 1, "left": -1, "east": 1, "west": -1}
_DIR_V = {"down": 1, "up": -1, "south": 1, "north": -1}
_MAG = [("a little", 60), ("little", 80), ("bit", 80), ("slightly", 80),
        ("small", 80), ("half", 100), ("quarter", 50), ("medium", 100),
        ("a lot", 500), ("lot", 400), ("much", 400), ("far", 500),
        ("way", 400), ("big", 400), ("double", 400), ("twice", 400)]


def parse_direction(text):
    t = " " + re.sub(r"[^a-z0-9 ]", " ", text.lower()) + " "
    t = t.replace(" upward ", " up ").replace(" downward ", " down ")
    t = t.replace(" upper ", " up ").replace(" lower ", " down ")
    t = t.replace(" top ", " up ").replace(" bottom ", " down ")
    h = v = 0
    for w, s in _DIR_H.items():
        if re.search(rf"\b{w}\b", t):
            h = s
            break
    for w, s in _DIR_V.items():
        if re.search(rf"\b{w}\b", t):
            v = s
            break
    if h and v:
        return (("up right" if v < 0 else "down right") if h > 0
                else ("up left" if v < 0 else "down left")), h, v
    if h:
        return ("right" if h > 0 else "left"), h, 0
    if v:
        return ("down" if v > 0 else "up"), 0, v
    return "", 0, 0


def parse_move_amount(text, default=200):
    t = text.lower()
    m = re.search(r"\b(\d{2,4})\b", t)
    if m:
        return max(5, min(3000, int(m.group(1))))
    for word, val in _MAG:
        if word in t:
            return val
    return default


def _set_cursor(x, y):
    u = ctypes.windll.user32
    u.SetCursorPos(int(max(2, min(x, 100000))), int(max(2, min(y, 100000))))


def move_mouse(direction, amount=None):
    name, h, v = parse_direction(direction)
    if not h and not v:
        speak("Which direction?")
        return
    if amount in (None, 0):
        amount = parse_move_amount(direction)

    screen_w, screen_h = pyautogui.size()
    x, y = _cursor_pos()
    dx, dy = h * amount, v * amount
    if h and v:
        dx, dy = int(dx * 0.707), int(dy * 0.707)

    nx = max(1, min(screen_w - 2, x + dx))
    ny = max(1, min(screen_h - 2, y + dy))

    if nx == x and ny == y:
        if v and not h:
            clicks = -12 if v > 0 else 12
            try:
                hw_scroll(clicks)
                speak(f"At the screen edge - scrolling {'down' if v > 0 else 'up'}")
            except Exception:
                speak("Cursor is already at the screen edge")
        else:
            speak("Cursor is already at the screen edge")
        return

    if _WIN32_OK:
        _set_cursor(nx, ny)
    else:
        pyautogui.moveTo(nx, ny)
    print(f"  [Move] {name} {amount}px: ({x},{y}) -> ({nx},{ny})")
    speak(f"Moving {name}", wait=False)


def click_here():
    pt = _cursor_pos()
    speak("Clicking here")
    smart_click(pt[0], pt[1])


def double_click_here():
    pt = _cursor_pos()
    speak("Double clicking here")
    reliable_double_click(pt[0], pt[1])


def right_click_here():
    pt = _cursor_pos()
    speak("Right clicking")
    right_click_at(pt[0], pt[1])


# =============================================================
#  Configuration
# =============================================================
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:1.5b"
OLLAMA_VISION_MODEL = "moondream:1.8b"

WAKE_WORDS = ["rolex", "hey rolex", "rolex mini", "rollex", "rolax",
              "rolix", "rollix", "rolexx"]

# Google STT mishears of "Rolex" - covered explicitly, plus a fuzzy rule below
WAKE_MISHEAR = [
    "relax", "relaxes", "rolexs", "roles", "rollx", "rolux", "robex",
    "relex", "rolaks", "rowlex", "roliks", "rolyx", "rolik", "rolers",
    "rolexx", "rolexy", "rolex on", "role x", "roll eggs", "raul x",
]


def _wake_ratio(tok):
    return SequenceMatcher(None, tok, "rolex").ratio()


def _is_fuzzy_wake_token(tok):
    """'relax', 'rolaks'... start with r, 4-8 chars, close to 'rolex'."""
    tok = tok.strip(".,!?;:'")
    if not (4 <= len(tok) <= 8):
        return False
    if not tok.startswith("r"):
        return False
    return _wake_ratio(tok) >= 0.78

AUTO_SLEEP_TIMEOUT = 120  # seconds of inactivity before auto-sleep

APP_MAP = {
    "chrome":          ("start", "chrome"),
    "google chrome":   ("start", "chrome"),
    "edge":            ("start", "msedge"),
    "microsoft edge":  ("start", "msedge"),
    "firefox":         ("start", "firefox"),
    "brave":           ("start", "brave"),
    "vlc":             ("run", r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
    "vlc player":      ("run", r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
    "media player":    ("start", "wmplayer"),
    "file explorer":   ("run", "explorer.exe"),
    "explorer":        ("run", "explorer.exe"),
    "files":           ("run", "explorer.exe"),
    "my computer":     ("run", "explorer.exe"),
    "this pc":         ("run", "explorer.exe"),
    "folder":          ("run", "explorer.exe"),
    "notepad":         ("run", "notepad.exe"),
    "calculator":      ("start", "calc"),
    "calc":            ("start", "calc"),
    "cmd":             ("run", "cmd.exe"),
    "command prompt":  ("run", "cmd.exe"),
    "terminal":        ("start", "wt"),
    "powershell":      ("run", "powershell.exe"),
    "task manager":    ("run", "taskmgr.exe"),
    "settings":        ("startfile", "ms-settings:"),
    "control panel":   ("run", "control.exe"),
    "paint":           ("run", "mspaint.exe"),
    "snipping tool":   ("start", "snippingtool"),
    "word":            ("start", "winword"),
    "excel":           ("start", "excel"),
    "powerpoint":      ("start", "powerpnt"),
    "spotify":         ("start", "spotify"),
    "discord":         ("start", "discord"),
    "telegram":        ("start", "telegram"),
    "whatsapp":        ("start", "whatsapp"),
    "youtube":         ("url", "https://www.youtube.com"),
    "gmail":           ("url", "https://mail.google.com"),
    "google":          ("url", "https://www.google.com"),
    "github":          ("url", "https://github.com"),
}

ORDINALS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "6th": 6,
    "seventh": 7, "7th": 7, "eighth": 8, "8th": 8, "ninth": 9, "9th": 9,
    "tenth": 10, "10th": 10,
}

# =============================================================
#  Redis cache (optional) + screenshot hash (ONE screenshot reused)
# =============================================================
try:
    import redis
    import hashlib

    _redis_client = redis.Redis(
        host="localhost", port=6379, db=0,
        decode_responses=False, socket_timeout=2, socket_connect_timeout=2,
    )
    _redis_client.ping()
    _REDIS_AVAILABLE = True
    print("  [OK] Redis connected - vision caching enabled")
except Exception as _e:
    _redis_client = None
    _REDIS_AVAILABLE = False
    print(f"  [!] Redis not available ({_e}) - using memory cache")


def _thumb_hash_from_image(img):
    thumb = img.resize((64, 36), Image.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=20)
    return hashlib.md5(buf.getvalue()).hexdigest()[:12]


def redis_cache_key(prefix, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    h = hashlib.md5(data).hexdigest()[:16]
    return f"rolex:{prefix}:{h}"


def redis_get(key):
    if not _REDIS_AVAILABLE:
        return None
    try:
        val = _redis_client.get(key)
        if val:
            return json.loads(val.decode("utf-8"))
    except Exception as e:
        print(f"  [!] Redis get error: {e}")
    return None


def redis_set(key, value, ttl=60):
    if not _REDIS_AVAILABLE:
        return
    try:
        _redis_client.setex(key, ttl, json.dumps(value).encode("utf-8"))
    except Exception as e:
        print(f"  [!] Redis set error: {e}")


def take_screenshot():
    """Single screenshot helper - reused by OCR and vision."""
    try:
        return pyautogui.screenshot()
    except Exception as e:
        print(f"  [!] Screenshot failed: {e}")
        return None


_cmd_cache = {}
_ollama_ok_cache = {"val": None, "time": 0}
_vision_ok_cache = {"val": None, "time": 0}


def cached_ollama(prompt):
    if _REDIS_AVAILABLE:
        rkey = redis_cache_key("ollama", prompt)
        cached = redis_get(rkey)
        if cached is not None:
            return cached.get("result")

    if prompt in _cmd_cache:
        return _cmd_cache[prompt]

    result = _ask_ollama_raw(prompt)
    if result:
        if len(_cmd_cache) > 200:
            _cmd_cache.clear()
        _cmd_cache[prompt] = result
        if _REDIS_AVAILABLE:
            rkey = redis_cache_key("ollama", prompt)
            redis_set(rkey, {"result": result}, ttl=300)

    return result


# =============================================================
#  Mic settings (dynamic channel detection)
# =============================================================
def get_mic_settings():
    try:
        dev_idx = sd.default.device[0]
        if dev_idx < 0:
            print("  [Mic] No default input device! Check Windows sound settings.")
            return 1, 44100, 0
        info = sd.query_devices(dev_idx)
        ch = min(int(info["max_input_channels"]), 2)
        sr = int(info["default_samplerate"])
        print(f"  [Mic] #{dev_idx}: {info['name']} ({ch}ch @ {sr}Hz)")
        print("  [Mic] If 'Rolex' never wakes, set THIS device as default in Windows sound settings.")
        return max(ch, 1), sr, dev_idx
    except Exception as e:
        print(f"  [!] Mic error: {e}")
        return 1, 44100, 0


MIC_CHANNELS, MIC_SAMPLE_RATE, MIC_DEVICE_IDX = get_mic_settings()

NOISE_FLOOR = 300
SILENCE_MULT = 1.4        # was 1.6 - wakes on quieter speech
MAX_RECORD_SECONDS = 10
SILENCE_SHORT = 0.35   # quick commands end fast
SILENCE_LONG  = 0.8    # longer commands


def _to_mono(data):
    """Average all channels. Laptop mic arrays often have a dead ch0."""
    if data.ndim > 1:
        return data.mean(axis=1)
    return data.flatten()


def calibrate_noise_floor(seconds=0.45):
    global NOISE_FLOOR
    try:
        print(f"  [*] Calibrating noise floor ({seconds}s)...")
        samples = int(MIC_SAMPLE_RATE * seconds)
        data = sd.rec(samples, samplerate=MIC_SAMPLE_RATE,
                      channels=MIC_CHANNELS, dtype="int16")
        sd.wait()
        mono = _to_mono(data).astype(np.float32)
        rms = float(np.sqrt(np.mean(mono ** 2)))
        NOISE_FLOOR = min(1500, max(80, int(rms * 1.15)))
        print(f"  [*] Noise floor: {NOISE_FLOOR}")
    except Exception as e:
        print(f"  [!] Calibration failed, using default: {e}")


def drain_mic():
    try:
        sd.rec(int(MIC_SAMPLE_RATE * 0.05),
               samplerate=MIC_SAMPLE_RATE,
               channels=MIC_CHANNELS, dtype="int16")
        sd.wait()
    except Exception:
        pass


# =============================================================
#  Thread-safe TTS (SAPI5 COM requires per-thread init)
#  + real "is speaking" event so listen() never hears its own voice
# =============================================================
import queue as _queue

_speech_queue = _queue.Queue()
_speech_thread = None
_speech_lock = threading.Lock()
_tts_pending = 0                     # number of utterances still being spoken
_tts_idle = threading.Event()        # set when nothing is speaking
_tts_idle.set()


def _speech_worker():
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass

    try:
        local_engine = pyttsx3.init()
        voices = local_engine.getProperty("voices")
        for v in voices:
            if "zira" in v.name.lower() or "david" in v.name.lower():
                local_engine.setProperty("voice", v.id)
                break
        local_engine.setProperty("rate", 190)
        local_engine.setProperty("volume", 0.9)
    except Exception as e:
        print(f"  [!] TTS worker init failed: {e}")
        with _speech_lock:
            _tts_idle.set()          # never leave callers blocked on wait_for_tts
        return

    while True:
        try:
            text = _speech_queue.get(timeout=60)
            if text is None:
                break
            try:
                local_engine.say(text)
                local_engine.runAndWait()
            except Exception as e:
                print(f"  [!] TTS error: {e}")
            finally:
                with _speech_lock:
                    global _tts_pending
                    _tts_pending -= 1
                    if _tts_pending <= 0:
                        _tts_pending = 0
                        _tts_idle.set()
        except _queue.Empty:
            continue

    try:
        local_engine.stop()
    except Exception:
        pass
    finally:
        with _speech_lock:
            _tts_idle.set()


def _ensure_speech_thread():
    global _speech_thread
    with _speech_lock:
        if _speech_thread is None or not _speech_thread.is_alive():
            _speech_thread = threading.Thread(target=_speech_worker, daemon=True)
            _speech_thread.start()


def speak(text, wait=False):
    print(f"  [Rolex] {text}")
    global _tts_pending
    _ensure_speech_thread()
    with _speech_lock:
        _tts_pending += 1
        _tts_idle.clear()
    _speech_queue.put(text)
    if wait:
        wait_for_tts()


def wait_for_tts(timeout=10.0):
    """Block until the engine has FINISHED speaking (not just drained queue)."""
    end = time.time() + timeout
    while not _tts_idle.wait(0.05):
        if time.time() > end:
            return False
    return True


print("  [*] TTS engine ready (thread-safe)")


# =============================================================
#  Audio Recording & STT
# =============================================================
def record_audio(max_seconds=MAX_RECORD_SECONDS):
    frames = []
    silence_count = 0
    speech_count = 0
    has_speech = False
    block_dur = 0.08
    block_size = int(MIC_SAMPLE_RATE * block_dur)
    max_blocks = int(max_seconds / block_dur)

    threshold = max(NOISE_FLOOR * SILENCE_MULT, 220)

    try:
        stream = sd.InputStream(
            samplerate=MIC_SAMPLE_RATE, channels=MIC_CHANNELS,
            dtype="int16", blocksize=block_size,
        )
        stream.start()
        for _ in range(max_blocks):
            data, _ = stream.read(block_size)
            mono = _to_mono(data).astype(np.float32)
            frames.append(mono.copy())
            rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))

            if rms > threshold:
                has_speech = True
                speech_count += 1
                silence_count = 0
            else:
                silence_count += 1

            speech_duration = speech_count * block_dur
            if speech_duration <= 1.5:
                silence_needed = int(SILENCE_SHORT / block_dur)
            else:
                silence_needed = int(SILENCE_LONG / block_dur)

            if has_speech and silence_count >= silence_needed:
                break

        stream.stop()
        stream.close()
        return np.concatenate(frames) if has_speech else None
    except Exception as e:
        print(f"  [!] Recording error: {e}")
        return None


def audio_to_wav_bytes(audio):
    if MIC_SAMPLE_RATE != 16000:
        ratio = 16000 / MIC_SAMPLE_RATE
        new_len = int(len(audio) * ratio)
        indices = np.linspace(0, len(audio) - 1, new_len).astype(int)
        audio = audio[indices]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()


try:
    import speech_recognition as _sr_module
except ImportError:
    _sr_module = None
    print("  [!] speech_recognition not installed")

_stt_fail_streak = {"n": 0}


def transcribe(audio):
    if _sr_module is None:
        print("  [!] speech_recognition missing: pip install SpeechRecognition")
        return None
    if audio is None or len(audio) < 1600:
        return None
    wav = audio_to_wav_bytes(audio)
    rec = _sr_module.Recognizer()
    rec.pause_threshold = 0.6
    rec.non_speaking_duration = 0.4
    data = _sr_module.AudioData(wav, 16000, 2)
    try:
        text = rec.recognize_google(data)
        _stt_fail_streak["n"] = 0
        return text.lower().strip()
    except _sr_module.UnknownValueError:
        print("  [STT] nothing intelligible")
        return None
    except _sr_module.RequestError as e:
        _stt_fail_streak["n"] += 1
        print(f"  [STT] Google unreachable ({e}) - streak={_stt_fail_streak['n']}")
        return None
    except Exception as e:
        print(f"  [!] Transcription error: {type(e).__name__}: {e}")
        return None


def listen(prompt=None, timeout=MAX_RECORD_SECONDS):
    if prompt:
        speak(prompt)
    wait_for_tts()          # don't record over our own voice
    drain_mic()
    print("  [*] Listening...")
    t0 = time.time()
    audio = record_audio(max_seconds=timeout)
    if audio is None:
        print("  [..] No speech detected")
        return None
    print(f"  [*] Recognizing... ({time.time()-t0:.1f}s audio)")
    text = transcribe(audio)
    if text:
        print(f"  [Heard] \"{text}\"  ({time.time()-t0:.1f}s total)")
    return text


recognize_speech = listen

# =============================================================
#  Continuous action control
# =============================================================
_continuous_action = {
    "running": False,
    "thread": None,
    "stop_flag": threading.Event(),
}

STOP_PHRASES = [
    "stop", "stop it", "stop now", "stop stop", "that's enough",
    "enough", "halt", "cease", "quit it", "okay stop", "ok stop",
    "stop scrolling", "stop moving", "pause", "freeze", "hold",
    "done", "that's good", "thats enough", "good", "perfect",
    "rolex stop", "stop rolex",
]

_STOP_WHOLE = {"stop", "stop it", "stop now", "stop stop", "ok stop", "okay stop",
               "enough", "that is enough", "thats enough", "halt", "cease",
               "quit it", "stop scrolling", "stop moving", "freeze", "stop please",
               "that is good", "thats good", "good enough", "rolex stop",
               "stop rolex", "cancel"}


def is_stop_command(text):
    if not text:
        return False
    t = _clean(text)
    if any(k in t for k in ("until", "till", "keep scrolling", "keep moving",
                            "forever", "continuously")):
        return False
    if re.search(r"\bhold\s+(?:down\s+)?(?:the\s+)?\w+\b", t):
        return False
    if t in _STOP_WHOLE:
        return True
    if len(t.split()) <= 3 and re.match(
            r"^(?:ok(?:ay)?\s+|please\s+|hey\s+)?(?:rolex\s+)?"
            r"(stop|halt|enough|cease|freeze)\b", t):
        return True
    return False


def stop_continuous_action():
    if _continuous_action["running"]:
        _continuous_action["stop_flag"].set()
        print("  [Stop] Stopping continuous action")
        return True
    return False


def handle_stop():
    if stop_continuous_action():
        speak("Stopped")
    else:
        speak("Nothing to stop")


def listen_for_stop_async():
    def _listen_loop():
        while _continuous_action["running"]:
            try:
                print("  [Listening for 'stop'...]")
                audio = record_audio(max_seconds=5)
                if audio is None:
                    continue
                text = transcribe(audio)
                if text:
                    print(f"  [Heard during action] \"{text}\"")
                    if is_stop_command(text):
                        stop_continuous_action()
                        speak("Stopped")
                        break
            except Exception as e:
                print(f"  [!] Stop listener error: {e}")
                break

    threading.Thread(target=_listen_loop, daemon=True).start()


def continuous_scroll(direction="down", speed="normal"):
    if _continuous_action["running"]:
        speak("Already running. Say stop first.")
        return

    speed_map = {
        "slow":   {"amount": 2, "delay": 0.5},
        "normal": {"amount": 5, "delay": 0.25},
        "fast":   {"amount": 10, "delay": 0.15},
    }
    cfg = speed_map.get(speed, speed_map["normal"])

    speak(f"Scrolling {direction}. Say stop when done.")
    time.sleep(0.3)

    _continuous_action["running"] = True
    _continuous_action["stop_flag"].clear()

    def _scroll_loop():
        try:
            direction_mult = 1 if direction == "up" else -1
            while not _continuous_action["stop_flag"].is_set():
                hw_scroll(cfg["amount"] * direction_mult)
                if _continuous_action["stop_flag"].wait(cfg["delay"]):
                    break
        finally:
            _continuous_action["running"] = False
            print("  [Scroll] Stopped")

    threading.Thread(target=_scroll_loop, daemon=True).start()
    listen_for_stop_async()


def continuous_move(direction, speed="normal"):
    if _continuous_action["running"]:
        speak("Already running. Say stop first.")
        return

    speed_map = {
        "slow":   {"step": 5,  "delay": 0.03},
        "normal": {"step": 10, "delay": 0.02},
        "fast":   {"step": 20, "delay": 0.015},
    }
    cfg = speed_map.get(speed, speed_map["normal"])

    step = cfg["step"]
    d = direction.lower().strip()
    d = d.replace("upper", "up").replace("lower", "down")

    dx, dy = 0, 0
    if "up" in d and "right" in d:   dx, dy = step, -step
    elif "up" in d and "left" in d:  dx, dy = -step, -step
    elif "down" in d and "right" in d: dx, dy = step, step
    elif "down" in d and "left" in d:  dx, dy = -step, step
    elif "right" in d:               dx = step
    elif "left" in d:                dx = -step
    elif "up" in d:                  dy = -step
    elif "down" in d:                dy = step
    else:
        speak(f"Unknown direction {direction}")
        return

    speak(f"Moving {direction}. Say stop when done.")
    time.sleep(0.3)

    _continuous_action["running"] = True
    _continuous_action["stop_flag"].clear()

    def _move_loop():
        try:
            screen_w, screen_h = pyautogui.size()
            while not _continuous_action["stop_flag"].is_set():
                x, y = _cursor_pos()
                nx = max(0, min(screen_w - 1, x + dx))
                ny = max(0, min(screen_h - 1, y + dy))
                if nx == x and ny == y:
                    print("  [Move] Hit screen edge, stopping")
                    break
                if _WIN32_OK:
                    _set_cursor(nx, ny)
                else:
                    pyautogui.moveTo(nx, ny)
                if _continuous_action["stop_flag"].wait(cfg["delay"]):
                    break
        finally:
            _continuous_action["running"] = False
            print("  [Move] Stopped")

    threading.Thread(target=_move_loop, daemon=True).start()
    listen_for_stop_async()


# =============================================================
#  Wake / Sleep / Exit logic
# =============================================================
EXIT_PHRASES = [
    "rolex exit", "exit rolex", "rolex quit", "quit rolex",
    "rolex close yourself", "close yourself rolex",
    "rolex shutdown", "shutdown rolex", "rolex shut down",
    "rolex close", "close rolex", "rolex goodbye", "goodbye rolex",
    "rolex terminate", "terminate rolex",
    "rolex turn off", "turn off rolex",
    "rolex power off", "power off rolex",
    "rolex kill yourself", "kill rolex",
    "rolex bye", "bye rolex",
    "rolex stop forever", "rolex die",
]

SLEEP_PHRASES = [
    "rolex sleep", "sleep rolex", "rolex go to sleep", "go to sleep rolex",
    "rolex sleep now", "sleep now rolex",
    "rolex stop listening", "stop listening rolex",
    "rolex pause", "pause rolex", "rolex pause listening",
    "rolex sleep mode", "sleep mode rolex",
    "rolex rest", "rest rolex",
    "rolex standby", "standby rolex", "rolex stand by",
    "rolex quiet mode", "quiet mode rolex",
    "rolex be quiet", "be quiet rolex",
    "rolex goodnight", "goodnight rolex", "rolex good night", "good night rolex",
    "rolex night", "night rolex",
    "rolex take a break", "take a break rolex",
    "rolex silent", "silent rolex",
]


def has_wake(text):
    if not text:
        return False
    t = " ".join(text.lower().split())
    for w in WAKE_WORDS + WAKE_MISHEAR:
        if re.search(r"\b" + re.escape(w) + r"\b", t):
            return True
        if t == w:
            return True
    # fuzzy fallback: any r-start token close enough to "rolex"
    for tok in t.split():
        if _is_fuzzy_wake_token(tok):
            return True
    return False


def strip_wake(text):
    if not text:
        return ""
    t = text.lower().strip()
    sorted_wake = sorted(WAKE_WORDS + WAKE_MISHEAR, key=len, reverse=True)

    for w in sorted_wake:
        if t.startswith(w + " ") or t == w:
            remaining = t[len(w):].strip()
            remaining = remaining.lstrip(",.!?;:")
            remaining = re.sub(r"^(please|can you|could you|would you|now)\s+", "", remaining)
            return remaining.strip()

    for w in sorted_wake:
        if t.endswith(" " + w):
            remaining = t[:-len(w)].strip()
            remaining = remaining.rstrip(",.!?;:")
            return remaining.strip()

    for w in sorted_wake:
        pattern = r"\b" + re.escape(w) + r"\b"
        if re.search(pattern, t):
            new = re.sub(pattern, "", t, count=1).strip()
            new = re.sub(r"\s+", " ", new)
            new = new.strip(",.!?;: ")
            return new

    # fuzzy fallback: drop r-start tokens that sound like "rolex"
    fuzzy_hit = [tok for tok in t.split() if _is_fuzzy_wake_token(tok)]
    if fuzzy_hit:
        cleaned = " ".join(tok for tok in t.split() if not _is_fuzzy_wake_token(tok))
        cleaned = re.sub(r"\s+", " ", cleaned).strip(",.!?;: ")
        return cleaned

    return t


_EXIT_WORDS = ("exit", "quit", "shutdown", "shut down", "terminate", "goodbye",
               "bye", "kill yourself", "close yourself", "turn off", "power off",
               "stop forever", "die", "shut yourself down")
_SCREEN_WORDS = ("click", "press", "tap", "open", "close", "tab", "window", "page",
                 "button", "link", "menu", "dialog", "save", "file", "search")
_SLEEP_WORDS = ("go to sleep", "sleep now", "sleep mode", "sleep", "goodnight",
                "good night", "standby", "stand by", "stop listening",
                "pause listening", "be quiet", "quiet mode", "silent mode",
                "take a break", "rest now")
_MEDIA_WORDS = ("video", "music", "song", "movie", "playback", "media", "volume",
                "playlist", "the track")


def _clean(text):
    t = text.lower().replace("'", "").replace("\u2019", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


_FILLERS = {"please", "now", "right", "immediately", "rolex", "can", "could",
            "you", "would", "just", "go", "and", "the", "program", "app",
            "application", "assistant", "for", "good", "ok", "okay"}


def _core_words(rest):
    return " ".join(w for w in rest.split() if w not in _FILLERS)


def is_exit(text):
    if not text or not has_wake(text):
        return False
    rest = _clean(strip_wake(text))
    if not rest:
        return False
    core = _core_words(rest)
    if core in ("exit", "quit", "shutdown", "shut down", "terminate", "goodbye",
                "bye", "kill yourself", "close yourself", "turn off", "power off",
                "stop forever", "die", "shut yourself down"):
        return True
    tails = {"the", "program", "app", "application", "yourself", "assistant",
             "everything", "down", "off", "out", "all", "completely"}
    for w in _EXIT_WORDS:
        if re.match(rf"^{re.escape(w)}\b", core):
            extra = core[len(w):].split()
            if all(e in tails for e in extra):
                return True
    if any(re.search(rf"\b{re.escape(w)}\b", rest) for w in _SCREEN_WORDS):
        return False
    return False


def is_sleep_command(text):
    if not text or not has_wake(text):
        return False
    rest = _clean(strip_wake(text))
    if not rest:
        return False
    if any(re.search(rf"\b{re.escape(w)}\b", rest) for w in _MEDIA_WORDS):
        return False
    if rest in ("pause", "rest", "quiet", "silent"):
        return True
    return any(re.search(rf"\b{re.escape(w)}\b", rest) for w in _SLEEP_WORDS)

# =============================================================
#  Ollama (optional, last resort only)
# =============================================================
def ollama_ok():
    if not ENABLE_LLM:
        return False
    now = time.time()
    if _ollama_ok_cache["val"] is not None and now - _ollama_ok_cache["time"] < 30:
        return _ollama_ok_cache["val"]
    try:
        import requests
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        val = len(r.json().get("models", [])) > 0
    except Exception:
        val = False
    _ollama_ok_cache["val"] = val
    _ollama_ok_cache["time"] = now
    return val


def _ask_ollama_raw(prompt):
    if not ENABLE_LLM:
        return None
    try:
        import requests
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt,
                  "stream": False, "options": {"temperature": 0.1, "num_predict": 30}},
            timeout=8,
        )
        if resp.status_code == 200:
            return resp.json().get("response", "").strip()
    except Exception as e:
        print(f"  [!] Ollama error: {e}")
    return None


def extract_keyword(command):
    prompt = (
        "Extract the single most important clickable word or short phrase from "
        f"this command. Reply with ONLY that word.\n\nCommand: \"{command}\"\nWord:"
    )
    result = cached_ollama(prompt)
    if result:
        result = result.split("\n")[0].strip().strip("\"'.,!").lower()
        if 0 < len(result) < 40:
            print(f"  [Ollama] keyword: \"{result}\"")
            return result
    return None


# ---- Ollama command translator (shrunk prompt, 8s timeout) ----
_translation_cache = {}

TRANSLATIONS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "learned_translations.json"
)


def load_translations():
    global _translation_cache
    try:
        if os.path.exists(TRANSLATIONS_FILE):
            with open(TRANSLATIONS_FILE, "r", encoding="utf-8") as f:
                _translation_cache = json.load(f)
            print(f"  [*] Loaded {len(_translation_cache)} learned translations")
    except Exception as e:
        print(f"  [!] Could not load translations: {e}")


def save_translations():
    try:
        with open(TRANSLATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(_translation_cache, f, indent=2)
    except Exception as e:
        print(f"  [!] Could not save translations: {e}")


KNOWN_COMMANDS_REFERENCE = """
KNOWN COMMAND FORMATS (translate user input to ONE of these):
open chrome | open vlc | open notepad | open calculator | open file explorer
open settings | open youtube | open 2nd chrome account
click [word] | click on [word] | click 2nd [word]
click here | double click | right click
go right | go left | go up | go down | go up right | go down left
go up a little | go down a lot | move left 300 | move right half
scroll up | scroll down | scroll up a lot | scroll down a little
scroll to top | scroll to bottom
play | pause | volume up | volume down | mute | forward | backward | fullscreen
go to tab 4 | next tab | previous tab | close tab | close 3rd tab | new tab
close this | minimize | maximize | restore this
minimize chrome | maximize vlc | close chrome | switch to chrome
minimize all | maximize all | show desktop | list windows
type [text] | search for [query] | google [query] | search youtube for [query]
press enter | press escape | press tab | go back | screenshot
click 2nd video in 3rd row | press ctrl c | press alt f4
scroll down until stop | move right until stop | stop | exit | go to sleep
"""


# Question / chit-chat detection: never worth an LLM round-trip
_QUESTION_START = (
    "what ", "whats ", "what's ", "who ", "when ", "where ", "why ", "how ",
    "is ", "are ", "was ", "were ", "do ", "does ", "did ", "can ", "could ",
    "would ", "will ", "should ", "tell me ", "explain ", "describe ",
    "weather ", "time ", "date ", "news ",
)


def looks_like_question(t):
    tt = t.strip().lower()
    return tt.endswith("?") or tt.startswith(_QUESTION_START)


def ollama_translate_command(user_text):
    cache_key = user_text.lower().strip()
    if cache_key in _translation_cache:
        cached = _translation_cache[cache_key]
        print(f"  [Learned] \"{user_text}\" -> \"{cached}\"")
        return cached

    if not ollama_ok():
        return None

    prompt = f"""Translate this voice command into ONE known format.
Reply with ONLY the translated command, or "unknown".

{KNOWN_COMMANDS_REFERENCE}

RULES: never reply with bare "scroll"; if unclear reply "unknown";
if it's a question, reply "unknown".

User: "take me to the right"     -> go right
User: "fire up spotify"          -> open spotify
User: "make this window go away" -> close this
User: "what time is it"          -> unknown

User: "{user_text}"
Translated:"""

    try:
        import requests
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt,
                  "stream": False,
                  "options": {"temperature": 0.1, "num_predict": 15}},
            timeout=8,
        )
        if resp.status_code == 200:
            answer = resp.json().get("response", "").strip()
            answer = answer.split("\n")[0].strip().strip("\"'.,!?").lower()
            for prefix in ("translated:", "command:", "answer:", "->"):
                if answer.startswith(prefix):
                    answer = answer[len(prefix):].strip()

            if answer and answer != "unknown" and len(answer) < 100:
                print(f"  [Ollama Translator] \"{user_text}\" -> \"{answer}\"")
                _translation_cache[cache_key] = answer
                save_translations()
                return answer
            else:
                print("  [Ollama Translator] Could not translate")
    except Exception as e:
        print(f"  [!] Translation error: {e}")

    return None


# =============================================================
#  Vision
# =============================================================
_vision_cache = {}
_detected_vision_model = None


def vision_model_ok():
    global _detected_vision_model
    if not ENABLE_LLM:
        return False
    now = time.time()
    if _vision_ok_cache["val"] is not None and now - _vision_ok_cache["time"] < 30:
        return _vision_ok_cache["val"]

    val = False
    try:
        import requests
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        models = r.json().get("models", [])
        for m in models:
            name = m.get("name", "")
            if "moondream" in name or "llava" in name or "bakllava" in name:
                _detected_vision_model = name
                val = True
                print(f"  [Vision] Using model: {name}")
                break
    except Exception:
        val = False

    _vision_ok_cache["val"] = val
    _vision_ok_cache["time"] = now
    return val


def find_image_on_screen(description):
    cache_key_desc = description.lower().strip()

    if _REDIS_AVAILABLE:
        shot = take_screenshot()
        if shot is not None:
            screen_hash = _thumb_hash_from_image(shot)
            rkey = redis_cache_key("vision", f"{cache_key_desc}:{screen_hash}")
            cached = redis_get(rkey)
            if cached is not None:
                if cached.get("found"):
                    coords = tuple(cached["coords"])
                    print(f"  [Redis Cache] Found \"{description}\" at {coords}")
                    return coords
                return None
            screenshot = shot
        else:
            screenshot = None
    else:
        screenshot = None

    if cache_key_desc in _vision_cache:
        mem = _vision_cache[cache_key_desc]
        if time.time() - mem["time"] < 10:
            return mem["coords"]

    print(f"  [Vision] Analyzing screen for: \"{description}\"")
    if screenshot is None:
        screenshot = take_screenshot()
    if screenshot is None:
        return None
    screen_w, screen_h = screenshot.size

    max_dim = 768
    if screen_w > max_dim or screen_h > max_dim:
        ratio = min(max_dim / screen_w, max_dim / screen_h)
        new_w, new_h = int(screen_w * ratio), int(screen_h * ratio)
        screenshot_small = screenshot.resize((new_w, new_h), Image.LANCZOS)
        scale_x, scale_y = screen_w / new_w, screen_h / new_h
    else:
        screenshot_small = screenshot
        scale_x = scale_y = 1.0
        new_w, new_h = screen_w, screen_h

    buf = io.BytesIO()
    screenshot_small.save(buf, format="JPEG", quality=60)
    img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    prompt = (
        f"Look at this screenshot ({new_w}x{new_h} pixels). "
        f"Find the \"{description}\" in the image. "
        f"Reply with ONLY the approximate pixel coordinates of its CENTER "
        f"in this format: x,y\n"
        f"If you cannot find it, reply: not found"
    )

    result_coords = None
    try:
        import requests
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": _detected_vision_model or OLLAMA_VISION_MODEL,
                  "prompt": prompt, "images": [img_b64], "stream": False,
                  "options": {"temperature": 0.1, "num_predict": 20}},
            timeout=25,
        )
        if resp.status_code == 200:
            answer = resp.json().get("response", "").strip()
            print(f"  [Vision] Raw answer: \"{answer}\"")
            if "not found" not in answer.lower():
                coord_match = re.search(r"(\d+)\s*[,;\s]\s*(\d+)", answer)
                if coord_match:
                    real_x = int(int(coord_match.group(1)) * scale_x)
                    real_y = int(int(coord_match.group(2)) * scale_y)
                    if 0 <= real_x <= screen_w and 0 <= real_y <= screen_h:
                        result_coords = (real_x, real_y)
                        print(f"  [Vision] Found at ({real_x}, {real_y})")
    except Exception as e:
        print(f"  [!] Vision error: {e}")

    _vision_cache[cache_key_desc] = {"coords": result_coords, "time": time.time()}

    if _REDIS_AVAILABLE:
        screen_hash = _thumb_hash_from_image(screenshot)
        rkey = redis_cache_key("vision", f"{cache_key_desc}:{screen_hash}")
        if result_coords:
            redis_set(rkey, {"found": True, "coords": list(result_coords)}, ttl=30)
        else:
            redis_set(rkey, {"found": False, "coords": None}, ttl=15)

    return result_coords


def is_image_query(text):
    image_words = ["image", "picture", "photo", "icon", "logo", "thumbnail",
                   "avatar", "emoji", "symbol", "graphic", "banner",
                   "the one with", "that looks like", "the picture of",
                   "that has", "showing"]
    t = text.lower()
    return any(w in t for w in image_words)


def extract_image_description(text):
    t = text.lower().strip()
    for prefix in ["click on", "click", "tap on", "tap", "select",
                   "open", "press", "the", "that", "a", "an"]:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    for suffix in ["image", "picture", "photo", "icon", "logo",
                   "thumbnail", "pic"]:
        if t.endswith(suffix):
            t = t[:-len(suffix)].strip()
    return t.strip() if t else text


# =============================================================
#  OCR
# =============================================================
try:
    import winocr as _winocr_module
    _WINOCR_AVAILABLE = True
except ImportError:
    _winocr_module = None
    _WINOCR_AVAILABLE = False
    print("  [!] winocr not installed - screen text reading disabled")

_ocr_cache = {"words": None, "time": 0}
_ocr_loop = None
OCR_CACHE_SECONDS = 1.5


def _get_ocr_loop():
    global _ocr_loop
    if _ocr_loop is None or _ocr_loop.is_closed():
        _ocr_loop = asyncio.new_event_loop()
    return _ocr_loop


def ocr_screen(force=False, screenshot=None):
    now = time.time()
    if (not force and _ocr_cache["words"] is not None
            and now - _ocr_cache["time"] < OCR_CACHE_SECONDS):
        return _ocr_cache["words"]

    if screenshot is None:
        screenshot = take_screenshot()
    if screenshot is None:
        return []

    screen_hash = None
    if not force and _REDIS_AVAILABLE:
        screen_hash = _thumb_hash_from_image(screenshot)
        rkey = redis_cache_key("ocr", screen_hash)
        cached = redis_get(rkey)
        if cached is not None:
            print(f"  [Redis OCR] cached OCR ({len(cached)} words)")
            _ocr_cache["words"] = cached
            _ocr_cache["time"] = now
            return cached

    words = []
    if not _WINOCR_AVAILABLE:
        _ocr_cache["words"] = words
        _ocr_cache["time"] = now
        return words

    try:
        loop = _get_ocr_loop()
        result = loop.run_until_complete(
            _winocr_module.recognize_pil(screenshot, lang="en")
        )
        for line in result.lines:
            for w in line.words:
                bb = w.bounding_rect
                words.append({
                    "text": w.text.lower(),
                    "x": int(bb.x + bb.width / 2),
                    "y": int(bb.y + bb.height / 2),
                    "w": int(bb.width),
                    "h": int(bb.height),
                    "raw": w.text,
                })
    except Exception as e:
        print(f"  [!] OCR error: {e}")

    _ocr_cache["words"] = words
    _ocr_cache["time"] = now

    if _REDIS_AVAILABLE and words:
        if screen_hash is None:
            screen_hash = _thumb_hash_from_image(screenshot)
        redis_set(redis_cache_key("ocr", screen_hash), words, ttl=10)

    return words


def find_on_screen(target, occurrence=1, prefer_region=None, force_ocr=False):
    words = ocr_screen(force=force_ocr)
    if not words:
        return None
    target_low = target.lower().strip()
    screen_h = pyautogui.size()[1]
    matches = []
    for w in words:
        if w["text"] == target_low:
            score = 1.0
        else:
            score = SequenceMatcher(None, target_low, w["text"]).ratio()
        if score > 0.6:
            matches.append((score, w))
    if not matches:
        return None
    matches.sort(key=lambda m: (m[1]["y"] // 20, m[1]["x"]))
    if prefer_region and len(matches) > 1:
        def bonus(w):
            if prefer_region == "top" and w["y"] < screen_h * 0.33:
                return 0.1
            if prefer_region == "bottom" and w["y"] > screen_h * 0.66:
                return 0.1
            return 0.0
        matches.sort(key=lambda m: -(m[0] + bonus(m[1])))
    occurrence = min(occurrence, len(matches))
    w = matches[occurrence - 1][1]
    print(f"  [OK] #{occurrence}: \"{w['text']}\" at ({w['x']},{w['y']})")
    return (w["x"], w["y"])


def find_grid_element(item_type, col_num, row_num=None):
    print(f"  [*] Finding {item_type} #{col_num}" + (f" in row {row_num}" if row_num else ""))
    words = ocr_screen()
    if not words:
        return None

    words.sort(key=lambda w: (w["y"], w["x"]))

    rows = []
    current_row = [words[0]]
    for w in words[1:]:
        if abs(w["y"] - current_row[-1]["y"]) < 40:
            current_row.append(w)
        else:
            rows.append(current_row)
            current_row = [w]
    rows.append(current_row)
    rows = [r for r in rows if len(r) >= 2]

    if row_num and row_num <= len(rows):
        target_row = rows[row_num - 1]
    elif not row_num:
        all_items = [w for row in rows for w in row]
        if col_num <= len(all_items):
            pick = all_items[col_num - 1]
            return (pick["x"], pick["y"])
        return None
    else:
        return None

    target_row.sort(key=lambda w: w["x"])
    if col_num <= len(target_row):
        pick = target_row[col_num - 1]
        return (pick["x"], pick["y"])
    return None

# =============================================================
#  Key name normalization
# =============================================================
_KEY_ALIASES = {
    "page down": "pagedown", "page up": "pageup",
    "caps lock": "capslock", "num lock": "numlock",
    "scroll lock": "scrolllock", "print screen": "printscreen",
    "left arrow": "left", "right arrow": "right",
    "up arrow": "up", "down arrow": "down",
    "space bar": "space", "spacebar": "space",
    "back space": "backspace", "back-space": "backspace",
    "return": "enter", "esc": "escape",
    "delete key": "delete", "del": "delete",
    "windows key": "win", "win key": "win",
    "control": "ctrl", "ctrl key": "ctrl",
    "alt key": "alt", "shift key": "shift",
    "function 1": "f1", "function 2": "f2", "function 3": "f3",
    "function 4": "f4", "function 5": "f5", "function 6": "f6",
    "function 7": "f7", "function 8": "f8", "function 9": "f9",
    "function 10": "f10", "function 11": "f11", "function 12": "f12",
}

KEY_NAME_MAP = {
    "enter": "enter", "return": "enter",
    "escape": "escape", "esc": "escape",
    "space": "space", "spacebar": "space",
    "tab": "tab",
    "backspace": "backspace", "back space": "backspace",
    "delete": "delete", "del": "delete",
    "insert": "insert", "ins": "insert",
    "home": "home", "end": "end",
    "page up": "pageup", "pageup": "pageup",
    "page down": "pagedown", "pagedown": "pagedown",
    "up": "up", "up arrow": "up", "arrow up": "up",
    "down": "down", "down arrow": "down", "arrow down": "down",
    "left": "left", "left arrow": "left", "arrow left": "left",
    "right": "right", "right arrow": "right", "arrow right": "right",
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4",
    "f5": "f5", "f6": "f6", "f7": "f7", "f8": "f8",
    "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
    "shift": "shift", "left shift": "shiftleft", "right shift": "shiftright",
    "control": "ctrl", "ctrl": "ctrl", "left control": "ctrlleft",
    "alt": "alt", "left alt": "altleft", "right alt": "altright",
    "windows": "win", "win": "win", "super": "win",
    "caps lock": "capslock", "caps": "capslock",
    "num lock": "numlock", "scroll lock": "scrolllock",
    "print screen": "printscreen", "prtsc": "printscreen",
    "period": ".", "dot": ".",
    "comma": ",",
    "slash": "/", "forward slash": "/",
    "backslash": "\\", "back slash": "\\",
    "semicolon": ";", "colon": ":",
    "quote": "'", "double quote": '"',
    "dash": "-", "minus": "-", "hyphen": "-",
    "plus": "+", "equals": "=", "equal": "=",
    "underscore": "_",
    "at": "@", "at sign": "@",
    "hash": "#", "hashtag": "#", "pound": "#",
    "dollar": "$", "percent": "%",
    "ampersand": "&", "and sign": "&",
    "asterisk": "*", "star": "*",
    "open bracket": "[", "close bracket": "]",
    "open brace": "{", "close brace": "}",
    "open paren": "(", "close paren": ")",
    "tilde": "~", "backtick": "`", "grave": "`",
    "pipe": "|",
    "exclamation": "!", "question mark": "?",
}

MODIFIER_MAP = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt",
    "shift": "shift",
    "win": "win", "windows": "win", "super": "win", "command": "win",
    "fn": "fn",
}


def normalize_key_name(name):
    n = name.lower().strip()
    if n in _KEY_ALIASES:
        return _KEY_ALIASES[n]
    joined = n.replace(" ", "")
    try:
        if joined in pyautogui.KEYBOARD_KEYS:
            return joined
    except Exception:
        pass
    return n


def resolve_key_name(spoken):
    s = spoken.lower().strip()
    if s in KEY_NAME_MAP:
        return KEY_NAME_MAP[s]
    if len(s) == 1 and s.isalpha():
        return s
    if len(s) == 1 and s.isdigit():
        return s
    m = re.match(r"(?:letter|key)\s+([a-z0-9])", s)
    if m:
        return m.group(1)
    m = re.match(r"function\s+(\d+)", s)
    if m:
        return f"f{m.group(1)}"
    return s


def parse_key_combo(text):
    t = text.lower().strip()
    t = t.replace("+", " ").replace("-", " ")
    t = t.replace(" and ", " ").replace(" plus ", " ").replace(" with ", " ")

    parts = t.split()
    keys = []
    modifiers = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part in MODIFIER_MAP:
            modifiers.append(MODIFIER_MAP[part])
        else:
            resolved = resolve_key_name(part)
            keys.append(resolved)
    return modifiers + keys


# =============================================================
#  Tab switching by number
# =============================================================
def switch_tab_number(num):
    try:
        n = int(num)
    except Exception:
        n = 1
    n = max(1, min(9, n))
    speak(f"Switching to tab {n}")
    pyautogui.hotkey("ctrl", str(n))


def next_tab():
    speak("Next tab")
    pyautogui.hotkey("ctrl", "tab")


def prev_tab():
    speak("Previous tab")
    pyautogui.hotkey("ctrl", "shift", "tab")


def close_nth_tab(num):
    try:
        n = int(num)
    except Exception:
        n = 1
    speak(f"Closing tab {n}")
    pyautogui.hotkey("ctrl", str(n))
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "w")


# =============================================================
#  Context detection
# =============================================================
def get_foreground_window_info():
    try:
        hwnd = _user32.GetForegroundWindow()
        cls_buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls_buf, 256)
        cls = cls_buf.value or ""
        length = _user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, title_buf, length + 1)
        title = title_buf.value or ""
        try:
            import psutil
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc = psutil.Process(pid.value).name().lower()
        except Exception:
            proc = ""
        return cls.lower(), title.lower(), proc
    except Exception:
        return "", "", ""


def get_auto_sleep_timeout():
    try:
        cls, title, proc = get_foreground_window_info()
        media_apps = ["vlc", "chrome", "msedge", "firefox", "spotify",
                      "brave", "opera", "mpv", "mpc-hc"]
        if any(m in proc for m in media_apps):
            return 300
    except Exception:
        pass
    return AUTO_SLEEP_TIMEOUT


def get_context():
    cls, title, proc = get_foreground_window_info()
    browser_procs = ("chrome.exe", "msedge.exe", "firefox.exe",
                     "brave.exe", "opera.exe", "vivaldi.exe")
    if any(bp in proc for bp in browser_procs):
        return "browser"
    if "chrome_widgetwin" in cls or "mozillawindowclass" in cls:
        return "browser"
    if "cabinetwclass" in cls or "explorewclass" in cls:
        return "explorer"
    if "progman" in cls or "workerw" in cls:
        return "desktop"
    if proc in ("notepad.exe", "notepad++.exe", "code.exe", "sublime_text.exe"):
        return "editor"
    return "other"


def is_url(text):
    t = text.lower().strip()
    if t.startswith(("http://", "https://", "www.")):
        return True
    if re.match(r"^[\w-]+\.[a-z]{2,}(/.*)?$", t):
        return True
    return False


# =============================================================
#  Command normalization  (THE FIX SECTION)
# =============================================================
_SHORTCUTS = [("select all", "ctrl a"), ("copy", "ctrl c"), ("paste", "ctrl v"),
              ("cut", "ctrl x"), ("undo", "ctrl z"), ("redo", "ctrl y"),
              ("save as", "ctrl shift s"), ("save", "ctrl s"), ("find", "ctrl f"),
              ("refresh", "f5"), ("reload", "f5")]


def apply_shortcuts(t):
    for word, combo in _SHORTCUTS:
        t = re.sub(rf"^\s*(?:press\s+|do\s+)?{re.escape(word)}"
                   rf"(?:\s+(?:now|please|it))?\s*$", f" press {combo} ", t)
    return t


def apply_path_prepositions(t):
    if re.search(r"(folder|directory|drive|disk|file explorer|explorer|documents|"
                 r"downloads|pictures|movies|music|videos)", t):
        for w in (" inside ", " within ", " under ", " into ", " from ", " of "):
            t = t.replace(w, " in ")
    return t


def _strip_polite_wrappers(t):
    """'can you please open chrome for me' -> ' open chrome '"""
    t = re.sub(r"^\s*(?:hey\s+)?(?:please|kindly|just)?\s*"
               r"(?:can you|could you|would you|will you|would you mind|"
               r"do you want to|i want you to|i need you to)\s+", " ", t)
    t = re.sub(r"^\s*(?:please|kindly|just)\s+", " ", t)
    t = re.sub(r"\b(?:please|for me|for me please|now|right now|right away|asap|ok|okay)\s*$", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t


def normalize_command(text):
    t = " " + text.lower().strip() + " "
    t = apply_shortcuts(t)

    # ---- Polite wrappers off FIRST so real rules can match ----
    t = _strip_polite_wrappers(t)

    # ---- Collapse 'verb (mouse|cursor|pointer) ... direction' ----
    # "move the mouse to the right" / "slide cursor right" / "drag pointer left"
    t = re.sub(r"\b(?:move|go|slide|shift|drag|push|pull|bring|place|put)\s+"
               r"(?:the\s+|this\s+|your\s+)?(?:mouse|cursor|pointer|arrow)\s+"
               r"(?:towards?\s+|to\s+|toward\s+|onto\s+)?(?:the\s+)?",
               " move ", t)
    # "mouse/cursor to the right" (bare subject form)
    t = re.sub(r"^\s*(?:the\s+)?(?:mouse|cursor|pointer)\s+"
               r"(?:towards?\s+|to\s+)?(?:the\s+)?"
               r"(right|left|up|down|up right|up left|down right|down left)\b",
               r" move \1 ", t)

    # "go to right" -> "go right" (strip stray 'to')
    t = re.sub(r"\b(go|move|cursor|mouse|shift|slide)\s+to\s+(the\s+)?"
               r"(right|left|up|down|upper|lower)\b", r"\1 \3", t)
    t = t.replace(" go to the right ", " go right ")
    t = t.replace(" go to the left ", " go left ")
    t = t.replace(" go to the top ", " go up ")
    t = t.replace(" go to the bottom ", " go down ")
    t = t.replace(" move to the right ", " move right ")
    t = t.replace(" move to the left ", " move left ")
    t = t.replace(" move to the top ", " move up ")
    t = t.replace(" move to the bottom ", " move down ")

    # ---- Click here synonyms (deduplicated) ----
    for pat in (" click right here ", " click at mouse ", " click where mouse is ",
                " click at cursor ", " click at the cursor ", " click where cursor is ",
                " tap right here ", " tap at cursor ", " tap at mouse ",
                " just click ", " click it ", " click now "):
        t = t.replace(pat, " click here ")

    # ---- Click synonyms  (NOTE: 'select' removed - it is its own action) ----
    for syn in ["click on", "click at", "tap on", "tap at", "press on",
                "hit on", "touch", "activate", "trigger", "pick", "choose on"]:
        t = t.replace(f" {syn} ", " click ")

    # ---- Open synonyms ----
    for syn in ["bring up", "pull up", "boot up", "fire up", "load up",
                "load", "launch", "start up", "boot"]:
        t = t.replace(f" {syn} ", " open ")

    # ---- Close synonyms ----
    for syn in ["shut", "terminate", "end", "get rid of",
                "remove", "exit out of", "exit"]:
        t = t.replace(f" {syn} this ", " close this ")
        t = t.replace(f" {syn} window ", " close window ")
        t = t.replace(f" {syn} tab ", " close tab ")
    t = t.replace(" shut down ", " close ")       # "shut down chrome" -> "close chrome"
    t = t.replace(" close down ", " close ")
    t = t.replace(" turn off ", " close ")        # "turn off vlc" -> "close vlc"

    # ---- Volume synonyms ----
    t = t.replace(" turn it up ", " volume up ")
    t = t.replace(" turn it down ", " volume down ")
    t = t.replace(" turn up the volume ", " volume up ")
    t = t.replace(" turn down the volume ", " volume down ")
    t = t.replace(" turn the volume up ", " volume up ")
    t = t.replace(" turn the volume down ", " volume down ")
    t = t.replace(" turn up volume ", " volume up ")
    t = t.replace(" turn down volume ", " volume down ")
    t = t.replace(" raise the volume ", " volume up ")
    t = t.replace(" lower the volume ", " volume down ")
    t = t.replace(" raise volume ", " volume up ")
    t = t.replace(" lower volume ", " volume down ")
    t = t.replace(" increase the volume ", " volume up ")
    t = t.replace(" decrease the volume ", " volume down ")
    t = t.replace(" make it louder ", " volume up ")
    t = t.replace(" make it quieter ", " volume down ")
    t = t.replace(" sound up ", " volume up ")
    t = t.replace(" sound down ", " volume down ")
    t = t.replace(" crank it ", " volume up ")
    t = t.replace(" silence ", " mute ")

    # ---- Media synonyms ----
    t = t.replace(" start playing ", " play ")
    t = t.replace(" start the video ", " play ")
    t = t.replace(" start video ", " play ")
    t = t.replace(" start music ", " play ")
    t = t.replace(" pause it ", " pause ")
    t = t.replace(" pause this ", " pause ")
    t = t.replace(" hold on ", " pause ")
    t = t.replace(" skip ahead ", " forward ")
    t = t.replace(" jump ahead ", " forward ")
    t = t.replace(" go further ", " forward ")

    # ---- Mouse movement hints ----
    t = t.replace(" cursor goes ", " move ")
    t = t.replace(" pointer ", " cursor ")
    t = t.replace(" bring cursor ", " move cursor ")
    t = t.replace(" shift cursor ", " move cursor ")
    t = t.replace(" drag cursor ", " move cursor ")

    # ---- Half synonyms ----
    for syn in [" halfway ", " half way ", " by half ", " fifty percent ",
                " 50 percent ", " medium amount ", " medium "]:
        t = t.replace(syn, " half ")

    # ---- Window state synonyms ----
    t = t.replace(" make this small ", " minimize this ")
    t = t.replace(" shrink this ", " minimize this ")
    t = t.replace(" shrink window ", " minimize this ")
    t = t.replace(" put away ", " minimize this ")
    t = t.replace(" hide this ", " minimize this ")
    t = t.replace(" minimize current window ", " minimize this ")
    t = t.replace(" minimize the window ", " minimize this ")
    t = t.replace(" make this big ", " maximize this ")
    t = t.replace(" make it big ", " maximize this ")
    t = t.replace(" make this large ", " maximize this ")
    t = t.replace(" enlarge this ", " maximize this ")
    t = t.replace(" expand this ", " maximize this ")
    t = t.replace(" expand window ", " maximize this ")
    t = t.replace(" maximize current window ", " maximize this ")
    t = t.replace(" maximize the window ", " maximize this ")
    t = t.replace(" blow up this window ", " maximize this ")
    t = t.replace(" grow this window ", " maximize this ")
    t = t.replace(" un maximize ", " restore this ")
    t = t.replace(" unmax ", " restore this ")
    t = t.replace(" normal size ", " restore this ")
    t = t.replace(" restore window ", " restore this ")
    t = t.replace(" restore the window ", " restore this ")

    # Fullscreen
    t = t.replace(" full screen ", " fullscreen ")
    t = t.replace(" full-screen ", " fullscreen ")

    # window standalone
    t = t.replace(" minimize window ", " minimize this ")
    t = t.replace(" maximize window ", " maximize this ")

    # ---- Continuous action synonyms ----
    t = t.replace(" until i tell you to stop ", " until stop ")
    t = t.replace(" until i tell you ", " until stop ")
    t = t.replace(" until i say stop ", " until stop ")
    t = t.replace(" until i say ", " until stop ")
    t = t.replace(" until you hear stop ", " until stop ")
    t = t.replace(" till i say ", " until stop ")
    t = t.replace(" and keep going ", " continuously ")
    t = t.replace(" without stopping ", " continuously ")
    t = t.replace(" non stop ", " continuously ")
    t = t.replace(" don't stop ", " continuously ")
    t = t.replace(" dont stop ", " continuously ")

    # ---- Stop synonyms ----
    t = t.replace(" cut it out ", " stop ")
    t = t.replace(" quit that ", " stop ")
    t = t.replace(" knock it off ", " stop ")
    t = t.replace(" that's good ", " stop ")
    t = t.replace(" thats good ", " stop ")
    t = t.replace(" good enough ", " stop ")

    # ---- Search synonyms ----
    t = t.replace(" look for ", " search for ")
    t = t.replace(" find info on ", " search for ")
    t = t.replace(" find me ", " search for ")
    t = t.replace(" google me ", " google ")

    # ---- Navigation ----
    t = t.replace(" previous page ", " go back ")
    t = t.replace(" last page ", " go back ")
    t = t.replace(" undo navigation ", " go back ")

    # ---- Screenshot ----
    t = t.replace(" take picture ", " screenshot ")
    t = t.replace(" capture screen ", " screenshot ")
    t = t.replace(" snap screen ", " screenshot ")
    t = t.replace(" print screen ", " screenshot ")

    # ---- Tabs ----
    t = t.replace(" fresh tab ", " new tab ")
    t = t.replace(" another tab ", " new tab ")
    t = t.replace(" open a tab ", " new tab ")
    t = t.replace(" new window tab ", " new tab ")

    # ---- Keyboard synonyms ----
    t = t.replace(" hit enter ", " press enter ")
    t = t.replace(" hit return ", " press enter ")
    t = t.replace(" submit ", " press enter ")
    t = t.replace(" confirm ", " press enter ")
    t = t.replace(" dismiss ", " press escape ")
    t = t.replace(" close dialog ", " press escape ")
    t = t.replace(" push ", " press ")
    t = t.replace(" smash ", " press ")
    t = t.replace(" strike ", " press ")
    t = t.replace(" hotkey ", " press ")
    t = t.replace(" key combo ", " press ")
    t = t.replace(" key combination ", " press ")
    t = t.replace(" shortcut ", " press ")

    # ---- Repeat words ----
    t = t.replace(" couple times ", " 2 times ")
    t = t.replace(" couple of times ", " 2 times ")
    t = t.replace(" few times ", " 3 times ")
    t = t.replace(" several times ", " 5 times ")
    t = t.replace(" many times ", " 10 times ")

    # ---- Windows ----
    t = t.replace(" show the desktop ", " show desktop ")
    t = t.replace(" hide everything ", " minimize all ")
    t = t.replace(" clear screen ", " minimize all ")
    t = t.replace(" bring back ", " restore ")
    t = t.replace(" what windows are open ", " list windows ")

    # ---- Sleep / exit synonyms ----
    t = t.replace(" take a nap ", " go to sleep ")
    t = t.replace(" nap time ", " go to sleep ")
    t = t.replace(" bedtime ", " go to sleep ")
    t = t.replace(" rest up ", " go to sleep ")
    t = t.replace(" chill out ", " go to sleep ")
    t = t.replace(" relax now ", " go to sleep ")
    t = t.replace(" shush ", " go to sleep ")
    t = t.replace(" hush ", " go to sleep ")
    t = t.replace(" dont listen ", " stop listening ")
    t = t.replace(" don't listen ", " stop listening ")
    t = t.replace(" stop hearing ", " stop listening ")
    t = t.replace(" ignore me ", " stop listening ")

    t = t.replace(" close program ", " exit ")
    t = t.replace(" close the program ", " exit ")
    t = t.replace(" close app ", " exit ")
    t = t.replace(" close application ", " exit ")
    t = t.replace(" kill program ", " exit ")
    t = t.replace(" end program ", " exit ")
    t = t.replace(" power down ", " shutdown ")
    t = t.replace(" power off ", " shutdown ")
    t = t.replace(" see ya ", " goodbye ")
    t = t.replace(" see you later ", " goodbye ")
    t = t.replace(" bye bye ", " goodbye ")
    t = t.replace(" catch you later ", " goodbye ")

    # ---- Multi-step connectors ----
    for conn in (" and after that ", " after that ", " afterwards ",
                 " following that ", " once done ", " when done ",
                 " once that's done ", " after this ", " subsequently "):
        t = t.replace(conn, " then ")

    t = apply_path_prepositions(t)

    t = t.replace(" slash ", " in ")
    t = t.replace(" backslash ", " in ")
    t = t.replace(" -> ", " in ")
    t = t.replace(" > ", " in ")

    # Remove leading/trailing "rolex" (wake word already handled)
    t = re.sub(r"^\s*rolex\s+", " ", t)
    t = re.sub(r"\s+rolex\s*$", " ", t)

    return t.strip()

# =============================================================
#  Multi-step planner
# =============================================================
_SIMPLE_STARTS = ("go", "move", "scroll", "cursor", "stop", "press", "type",
                  "next", "previous", "prev", "play", "pause", "volume", "mute",
                  "minimize", "maximize", "restore", "screenshot")


def is_multi_step_command(text):
    t = re.sub(r"\s+", " ", text.lower().strip())
    if re.search(r"\b(?:next|previous|prev)\s+tab\b", t):
        return False
    if re.search(r"\b(?:go|move)\s+(?:up|down|left|right|up right|down left)\b", t):
        return False
    if re.search(r"\b(?:open|close|minimize|maximize|click|launch)\b.+\band\b.+"
                 r"\b(?:open|close|minimize|maximize|click|launch|play|type)\b", t):
        return True
    if re.match(rf"^(?:{'|'.join(_SIMPLE_STARTS)})\b", t) and \
       not re.search(r"\b(?:then|and then|after that)\b", t):
        return False
    if re.search(r"\b(?:then|and then|after that|afterwards|following that)\b", t):
        return True
    if t.count(" in ") >= 2 and re.search(
            r"(folder|directory|drive|disk|file explorer|explorer|documents|"
            r"downloads|pictures|movies|music|videos)", t):
        return True
    if re.search(r"open\s+.+\s+in\s+.+\s+in\s+", t):
        return True
    if re.search(r"(?:open|find|go to)\s+.+\s+in\s+.+\s*(?:folder|directory|"
                 r"documents|downloads|desktop|pictures|movies|music|videos|"
                 r"drive|disk)", t):
        return True
    if re.search(r"open\s+\w+\s+and\s+(search|type|go to|find|play)", t):
        return True
    if re.search(r"(find|search for|look for)\s+.+\s+in\s+(file explorer|chrome|"
                 r"browser|explorer|downloads|documents|desktop|movies|music)", t):
        return True
    if re.search(r"(open|close|minimize|maximize|click)\s+.+\s+and\s+"
                 r"(open|close|minimize|maximize|click)", t):
        return True
    return False


def plan_steps_with_ollama(user_text):
    if not ollama_ok():
        return None

    ctx = get_context()
    cls, title, proc = get_foreground_window_info()
    context_info = f"Currently active: {ctx}"
    if title:
        context_info += f" (window title: '{title[:50]}')"

    prompt = f"""You are a step planner for a voice assistant called Rolex on Windows.
Break the user's complex command into a numbered list of SIMPLE steps.

{KNOWN_COMMANDS_REFERENCE}

CONTEXT: {context_info}

RULES:
1. Each step must be ONE simple command from the list above.
2. Add "wait 1" between steps that need time to load, "wait 2" for slow apps.
3. For File Explorer folder navigation: click [foldername] for each folder.
4. For browser: press ctrl l / press ctrl t, then type [text], then press enter.
5. Numbered list only, one step per line. No explanations.

EXAMPLE:
User: "open resume in documents folder"
Steps:
1. open file explorer
2. wait 2
3. click documents
4. wait 1
5. click resume

User: "{user_text}"
Steps:"""

    try:
        import requests
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt,
                  "stream": False,
                  "options": {"temperature": 0.1, "num_predict": 150}},
            timeout=20,
        )
        if resp.status_code == 200:
            answer = resp.json().get("response", "").strip()
            print(f"  [Planner] Raw plan:\n{answer}")
            return parse_plan(answer)
    except Exception as e:
        print(f"  [!] Planner error: {e}")

    return None


def parse_plan(plan_text):
    steps = []
    for line in plan_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        line = re.sub(r"^[-•]\s*", "", line).strip()
        if not line:
            continue
        skip_words = ["steps:", "plan:", "here", "following", "note:",
                      "explanation", "first,", "the steps"]
        if any(line.lower().startswith(sw) for sw in skip_words):
            continue

        wait_match = re.match(r"wait\s+(\d+)", line.lower())
        if wait_match:
            steps.append({"type": "wait", "seconds": int(wait_match.group(1)), "raw": line})
            continue

        if line.lower().startswith("confirm"):
            remaining = re.sub(r"^confirm\s*", "", line, flags=re.IGNORECASE).strip()
            steps.append({"type": "confirm",
                          "message": remaining or "Should I continue?",
                          "raw": line})
            continue

        steps.append({"type": "command", "command": line.lower().strip(), "raw": line})

    return steps if steps else None


def plan_should_cancel():
    try:
        return bool(_continuous_action.get("running")) and \
            _continuous_action["stop_flag"].is_set()
    except Exception:
        return False


def execute_plan(steps, user_text=""):
    if not steps:
        speak("No steps to execute")
        return

    total = len([s for s in steps if s["type"] == "command"])
    cmd_num = 0

    speak(f"I'll do this in {total} steps")
    time.sleep(0.3)

    for step in steps:
        if plan_should_cancel():
            speak("Plan cancelled")
            return

        if step["type"] == "wait":
            secs = step["seconds"]
            print(f"  [Plan] Waiting {secs}s...")
            time.sleep(secs)
            continue

        if step["type"] == "confirm":
            msg = step.get("message", "Should I continue?")
            speak(msg, wait=True)
            response = listen(timeout=8)
            if response:
                r = response.lower()
                if any(w in r for w in ["no", "stop", "cancel", "quit", "abort", "don't"]):
                    speak("Stopping here")
                    return
                if any(w in r for w in ["skip", "next", "pass"]):
                    speak("Skipping")
                    continue
            continue

        if step["type"] == "command":
            cmd_num += 1
            cmd = step["command"]
            print(f"\n  [Plan Step {cmd_num}/{total}] {cmd}")
            try:
                intent = parse_command(cmd, use_fallback=False)
                action = intent.get("action", "")

                if action == "unknown":
                    speak(f"Step {cmd_num} unclear: {cmd}. Skip or retry?", wait=True)
                    response = listen(timeout=8)
                    if response:
                        r = response.lower()
                        if any(w in r for w in ["skip", "next", "pass"]):
                            speak("Skipping")
                            continue
                        if any(w in r for w in ["stop", "cancel", "quit", "abort"]):
                            speak("Stopping here")
                            return
                        if any(w in r for w in ["retry", "try again", "again"]):
                            intent = parse_command(cmd, use_fallback=True)
                            if intent.get("action") != "unknown":
                                execute(intent)
                            else:
                                speak("Still can't understand. Skipping.")
                            continue
                    continue

                execute(intent)
                time.sleep(0.3)

            except Exception as e:
                print(f"  [!] Step {cmd_num} failed: {e}")
                speak(f"Step {cmd_num} failed. Continue or stop?")
                response = listen(timeout=8)
                if response and any(w in response.lower() for w in ["stop", "cancel", "quit"]):
                    speak("Stopping")
                    return
                continue

    speak("All done!")
    print("  [Plan] All steps completed")


def plan_locally(text):
    t = normalize_command(text)
    steps = []
    ctx = get_context()

    folder_match = re.match(r"(?:open|find|go to|navigate to)\s+(.+?)\s+in\s+(.+)", t)
    if folder_match:
        target = folder_match.group(1).strip()
        path = folder_match.group(2).strip()
        parts = [p.strip() for p in path.split(" in ")]
        parts = [p for p in parts if p]

        need_open_explorer = False
        cleaned_parts = []
        for p in parts:
            if p in ("file explorer", "explorer", "my computer", "this pc", "files"):
                need_open_explorer = True
            else:
                cleaned_parts.append(p)

        if ctx != "explorer" and not need_open_explorer:
            need_open_explorer = True

        if need_open_explorer:
            steps.append({"type": "command", "command": "open file explorer", "raw": "open file explorer"})
            steps.append({"type": "wait", "seconds": 2, "raw": "wait 2"})

        for folder in cleaned_parts:
            folder = folder.strip().rstrip(".")
            if folder in ("folder", "directory", "drive", "disk"):
                continue
            folder = re.sub(r"\s*(folder|directory|dir)\s*$", "", folder).strip()
            if folder:
                steps.append({"type": "command", "command": f"click {folder}", "raw": f"click {folder}"})
                steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})

        target = re.sub(r"\s*(folder|directory|file)\s*$", "", target).strip()
        if target:
            steps.append({"type": "command", "command": f"click {target}", "raw": f"click {target}"})

        return steps if len(steps) > 1 else None

    browser_match = re.match(
        r"(?:open|launch)\s+(\w+)\s+and\s+(?:search\s+for|search|find|look\s+for|type|go\s+to)\s+(.+)", t)
    if browser_match:
        app = browser_match.group(1).strip()
        query = browser_match.group(2).strip()

        steps.append({"type": "command", "command": f"open {app}", "raw": f"open {app}"})
        steps.append({"type": "wait", "seconds": 2, "raw": "wait 2"})

        if app in ("chrome", "edge", "firefox", "brave", "browser"):
            steps.append({"type": "command", "command": "press ctrl l", "raw": "press ctrl l"})
            steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})
            steps.append({"type": "command", "command": f"type {query}", "raw": f"type {query}"})
            steps.append({"type": "command", "command": "press enter", "raw": "press enter"})
        else:
            steps.append({"type": "command", "command": f"type {query}", "raw": f"type {query}"})
            steps.append({"type": "command", "command": "press enter", "raw": "press enter"})

        return steps if len(steps) > 1 else None

    site_search = re.match(
        r"(?:search\s+for|search|find|look\s+for)\s+(.+?)\s+(?:on|in)\s+(youtube|google|amazon|github|spotify|netflix)", t)
    if site_search:
        query = site_search.group(1).strip()
        site = site_search.group(2).strip()

        site_urls = {
            "youtube": "youtube.com", "google": "google.com", "amazon": "amazon.com",
            "github": "github.com", "spotify": "open.spotify.com", "netflix": "netflix.com",
        }
        url = site_urls.get(site, f"{site}.com")

        if ctx != "browser":
            steps.append({"type": "command", "command": "open chrome", "raw": "open chrome"})
            steps.append({"type": "wait", "seconds": 2, "raw": "wait 2"})

        steps.append({"type": "command", "command": "press ctrl t", "raw": "press ctrl t"})
        steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})
        steps.append({"type": "command", "command": f"type {url}", "raw": f"type {url}"})
        steps.append({"type": "command", "command": "press enter", "raw": "press enter"})
        steps.append({"type": "wait", "seconds": 2, "raw": "wait 2"})

        if site == "youtube":
            steps.append({"type": "command", "command": "click search", "raw": "click search"})
        else:
            steps.append({"type": "command", "command": "press ctrl l", "raw": "press ctrl l"})
        steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})
        steps.append({"type": "command", "command": f"type {query}", "raw": f"type {query}"})
        steps.append({"type": "command", "command": "press enter", "raw": "press enter"})

        return steps if len(steps) > 1 else None

    if re.search(r"\b(then|and then|after that|afterwards|next)\b", t):
        parts = re.split(r"\b(?:then|and then|after that|afterwards|next|,\s*then)\b", t)
        parts = [p.strip().rstrip(".").strip() for p in parts if p.strip()]
        if len(parts) >= 2:
            for p in parts:
                steps.append({"type": "command", "command": p, "raw": p})
                steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})
            return steps if len(steps) > 1 else None

    if re.search(r"(open|close|minimize|maximize|click|launch)\s+.+\s+and\s+(open|close|minimize|maximize|click|launch)", t):
        parts = re.split(r"\s+and\s+", t)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) >= 2:
            for p in parts:
                steps.append({"type": "command", "command": p, "raw": p})
                steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})
            return steps if len(steps) > 1 else None

    go_folder = re.match(r"(?:go to|navigate to|open)\s+(?:the\s+)?(.+?)\s+folder", t)
    if go_folder and ctx == "explorer":
        folder = go_folder.group(1).strip()
        folders = re.split(r"\s+(?:then|and then|>|/|\\)\s+", folder)
        for f in folders:
            f = f.strip()
            if f:
                steps.append({"type": "command", "command": f"click {f}", "raw": f"click {f}"})
                steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})
        return steps if len(steps) > 1 else None

    return None


_TAB_NUM = re.compile(r"(?:go to|switch to|jump to|change to|move to|open)\s+"
                      r"(?:the\s+)?(?:tab\s+(\d+)|(\d+)(?:st|nd|rd|th)?\s+tab)")
_TAB_ORD = re.compile(r"(?:go to|switch to|jump to|change to|move to|open)\s+"
                      r"(?:the\s+)?(\w+)\s+tab\b")
_TAB_BARE = re.compile(r"^tab\s+(\d+)$")


def parse_tab(text):
    t = re.sub(r"\s+", " ", text.lower().strip())
    for rx in (_TAB_NUM, _TAB_BARE):
        m = rx.search(t)
        if m:
            n = int([g for g in m.groups() if g and g.isdigit()][0])
            if 1 <= n <= 9:
                return ("number", n)
    m = _TAB_ORD.search(t)
    if m:
        word = m.group(1)
        n = ORDINALS.get(word) or (int(word) if word.isdigit() else None)
        if n and 1 <= n <= 9:
            return ("number", n)
        if word not in ("the", "a", "this", "that", "bar"):
            return ("name", word)
    return None


_RE_BACKWARD = re.compile(r"(rewind|backward|skip back|previous chapter|"
                          r"back\s+\d+\s*(?:seconds|secs|s)\b)")
_RE_FORWARD = re.compile(r"(fast.?forward|skip forward|skip ahead|"
                         r"forward\s+\d+\s*(?:seconds|secs|s)\b)")
_RE_GO_BACK = re.compile(r"\b(?:go|browser)\s+back\b|^\s*back\s*$|\bprevious\s+page\b")


def safe_last_resort(normalized_text, raw_text):
    clean = normalized_text.strip()
    if len(clean.split()) > 3 or re.match(r"^(the|a|an|is|am|are|was|were)$", clean):
        print(f"  [Unknown] no rule matched: \"{raw_text}\"")
        return {"action": "unknown", "target": raw_text}
    return {"action": "click_word", "target": clean, "occurrence": 1}


def extract_repeat(text):
    m_adv = re.search(r"\b(twice|thrice)\s*$", text)
    if m_adv:
        count = 2 if m_adv.group(1) == "twice" else 3
        cleaned = text[:m_adv.start()].strip()
        return cleaned, count
    m = re.search(r"\b([a-z0-9]+)\s+times?\s*$", text)
    if m:
        num_str = m.group(1)
        if num_str.isdigit():
            count = int(num_str)
        else:
            val = parse_number_words(num_str)
            count = val if val is not None else 1
        if count > 1:
            cleaned = text[:m.start()].strip()
            return cleaned, count
    return text, 1


def parse_number_words(text):
    m = re.search(r"\b(\d+)\b", text)
    if m:
        return int(m.group(1))
    clean = re.sub(r"\bseconds?\b|\bsecs?\b", "", text)
    word_map = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
        "fifty": 50, "sixty": 60,
    }
    for word, num in word_map.items():
        if re.search(r"\b" + re.escape(word) + r"\b", clean):
            return num
    for word, num in ORDINALS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", clean):
            return num
    return None

# =============================================================
#  parse_command
# =============================================================
def parse_command(text, use_fallback=True):
    debug_log(f"parse_command input: '{text}'")
    text_norm = normalize_command(text)
    debug_log(f"after normalize: '{text_norm}'")
    original = text_norm.lower().strip()
    t, repeat = extract_repeat(original)
    intent = _parse_command_impl(t, text=text, use_fallback=use_fallback)
    if intent and isinstance(intent, dict) and repeat > 1:
        intent["repeat"] = repeat
    return intent


def _parse_command_impl(t, text=None, use_fallback=True):
    if text is None:
        text = t

    def wrap(intent):
        return intent

    # ---- Multi-step detection (FIRST, but only genuinely complex ones) ----
    if use_fallback and is_multi_step_command(text):
        print(f"  [Multi-Step] Detected complex command: \"{text}\"")
        local_plan = plan_locally(text)
        if local_plan:
            print(f"  [Local Plan] {len(local_plan)} steps")
            for s in local_plan:
                print(f"    -> {s['raw']}")
            return {"action": "execute_plan", "target": text, "steps": local_plan}
        if ollama_ok():
            ollama_plan = plan_steps_with_ollama(text)
            if ollama_plan:
                print(f"  [Ollama Plan] {len(ollama_plan)} steps")
                for s in ollama_plan:
                    print(f"    -> {s['raw']}")
                return {"action": "execute_plan", "target": text, "steps": ollama_plan}
        print("  [Multi-Step] Could not plan, trying as single command")

    if t in ("show learned", "list translations", "what have you learned"):
        return {"action": "show_translations", "target": ""}

    # ---- STOP (highest priority) ----
    if is_stop_command(t):
        return {"action": "stop_continuous", "target": ""}

    # ---- Continuous scroll ----
    continuous_scroll_match = re.search(
        r"(?:keep\s+)?scroll(?:ing)?\s+(up|down)\s+"
        r"(?:until|till|while|and keep going|continuously|forever|non-?stop)", t)
    if continuous_scroll_match:
        direction = continuous_scroll_match.group(1)
        speed = "normal"
        if re.search(r"(slow|slowly)", t):
            speed = "slow"
        elif re.search(r"(fast|quickly|quick)", t):
            speed = "fast"
        return {"action": "continuous_scroll", "target": direction, "speed": speed}

    m = re.search(r"keep\s+scroll(?:ing)?\s+(up|down)", t)
    if m:
        speed = "normal"
        if re.search(r"(slow|slowly)", t):
            speed = "slow"
        elif re.search(r"(fast|quickly|quick)", t):
            speed = "fast"
        return {"action": "continuous_scroll", "target": m.group(1), "speed": speed}

    m = re.search(r"scroll\s+(up|down)\s+(forever|continuously|non-?stop|always)", t)
    if m:
        return {"action": "continuous_scroll", "target": m.group(1), "speed": "normal"}

    # ---- Continuous mouse move ----
    continuous_move_match = re.search(
        r"(?:keep\s+)?(?:move|go|moving|going)\s+"
        r"(up right|upper right|up left|upper left|"
        r"down right|lower right|down left|lower left|"
        r"right|left|up|down)\s+"
        r"(?:until|till|while|and keep going|continuously|forever|non-?stop)", t)
    if continuous_move_match:
        direction = continuous_move_match.group(1)
        speed = "normal"
        if re.search(r"(slow|slowly)", t):
            speed = "slow"
        elif re.search(r"(fast|quickly|quick)", t):
            speed = "fast"
        return {"action": "continuous_move", "target": direction, "speed": speed}

    m = re.search(
        r"keep\s+(?:moving|going)\s+"
        r"(up right|upper right|up left|upper left|"
        r"down right|lower right|down left|lower left|"
        r"right|left|up|down)", t)
    if m:
        speed = "normal"
        if re.search(r"(slow|slowly)", t):
            speed = "slow"
        elif re.search(r"(fast|quickly|quick)", t):
            speed = "fast"
        return {"action": "continuous_move", "target": m.group(1), "speed": speed}

    m = re.search(
        r"(?:move|go)\s+"
        r"(up right|upper right|up left|upper left|"
        r"down right|lower right|down left|lower left|"
        r"right|left|up|down)\s+(forever|continuously|non-?stop|always)", t)
    if m:
        return {"action": "continuous_move", "target": m.group(1), "speed": "normal"}

    # ---- Chrome with account ----
    m = re.search(
        r"(?:open|launch|start)\s+chrome\s+(?:with\s+)?(?:account\s+)?"
        r"(\w+)(?:\s+account)?", t)
    if m:
        word = m.group(1)
        num = ORDINALS.get(word) or (int(word) if word.isdigit() else None)
        if num:
            return {"action": "chrome_account", "target": str(num)}
    if re.search(r"(?:open|launch|start)\s+(?:the\s+)?(\w+)\s+(?:chrome\s+)?account", t):
        m2 = re.search(r"(?:open|launch|start)\s+(?:the\s+)?(\w+)\s+(?:chrome\s+)?account", t)
        word = m2.group(1)
        num = ORDINALS.get(word) or (int(word) if word.isdigit() else None)
        if num:
            return {"action": "chrome_account", "target": str(num)}

    # ---- URL / site navigation ("go to youtube.com", "visit github.com") ----
    m = re.match(r"(?:go to|open|visit|navigate to|load|browse)\s+(.+)$", t)
    if m:
        target = m.group(1).strip().rstrip(".")
        if is_url(target):
            return {"action": "open_url", "target": target}
        site_domains = {
            "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com",
            "google": "https://www.google.com", "github": "https://github.com",
            "maps": "https://maps.google.com", "facebook": "https://www.facebook.com",
            "twitter": "https://twitter.com", "x": "https://x.com",
            "instagram": "https://www.instagram.com", "reddit": "https://reddit.com",
            "wikipedia": "https://www.wikipedia.org",
        }
        if target in site_domains:
            return {"action": "open_url", "target": site_domains[target]}

    # ---- Tab switching ----
    tab_res = parse_tab(t)
    if tab_res:
        kind, val = tab_res
        if kind == "number":
            return {"action": "switch_tab_number", "target": str(val)}
        return {"action": "click_tab", "target": val}

    if re.search(r"\bnext\s+tab\b", t):
        return {"action": "next_tab", "target": ""}
    if re.search(r"\b(previous|prev|last)\s+tab\b", t):
        return {"action": "prev_tab", "target": ""}

    close_tab_match = re.search(r"close\s+(?:the\s+)?(?:(\w+)\s+)?tab\b", t)
    if close_tab_match:
        word = close_tab_match.group(1)
        if word:
            num = ORDINALS.get(word) or (int(word) if word.isdigit() else None)
            if num and 1 <= num <= 9:
                return {"action": "close_nth_tab", "target": str(num)}
            if word in ("this", "current", "the"):
                return {"action": "close_tab", "target": ""}
        return {"action": "close_tab", "target": ""}

    m = re.search(r"(?:go to|switch to|open|click|jump to)\s+(?:the\s+)?(.+?)\s+tab\b", t)
    if m:
        name = m.group(1).strip()
        if name not in ORDINALS and not name.isdigit():
            return {"action": "click_tab", "target": name}
    m = re.search(r"\btab\s+(?:called|named)\s+(.+)", t)
    if m:
        return {"action": "click_tab", "target": m.group(1).strip()}

    # ---- Taskbar / Window management ----
    if re.search(r"close\s+all\s+minimized", t):
        return {"action": "close_all_minimized", "target": ""}

    m = re.search(r"minimize\s+all\s+(\w+)", t)
    if m:
        name = m.group(1).strip()
        if name not in ("windows", "all"):
            return {"action": "minimize_window_by_name", "target": name, "occurrence": "all"}

    m = re.search(r"maximize\s+all\s+(\w+)", t)
    if m:
        name = m.group(1).strip()
        if name not in ("windows", "all"):
            return {"action": "maximize_window_by_name", "target": name, "occurrence": "all"}

    if re.search(r"\b(minimize all|show desktop|hide all|hide everything)\b", t):
        return {"action": "minimize_all", "target": ""}
    if re.search(r"\bmaximize all\b", t):
        return {"action": "maximize_all", "target": ""}
    if re.search(r"(restore all|bring back all|unminimize all|show all)", t):
        return {"action": "restore_all_minimized", "target": ""}
    if re.search(r"(list windows|what.?s open|show windows|open windows)", t):
        return {"action": "list_windows", "target": ""}

    m = re.search(r"close\s+all\s+(\w+)(?:\s+windows?)?", t)
    if m:
        name = m.group(1).strip()
        if name not in ("minimized", "windows", "tabs"):
            return {"action": "close_window_by_name", "target": name, "occurrence": "all"}

    # "close chrome" / "close vlc" / ... (single app name, no ordinal)
    m = re.match(r"^close\s+(?:the\s+)?(\w+)$", t)
    if m:
        name = m.group(1).strip()
        if name in APP_MAP or name not in ("this", "it", "current", "now",
                                           "all", "everything", "tab", "window"):
            return {"action": "close_window_by_name", "target": name, "occurrence": 1}

    # "close 2nd chrome" / "close second chrome window"
    m = re.search(r"close\s+(?:the\s+)?(\w+)\s+(\w+?)(?:\s+window)?$", t)
    if m:
        ord_word, name = m.group(1), m.group(2).strip()
        num = ORDINALS.get(ord_word) or (int(ord_word) if ord_word.isdigit() else None)
        if num and (name in APP_MAP or (len(name) > 2 and name not in ("window", "tab", "this"))):
            return {"action": "close_window_by_name", "target": name, "occurrence": num}

    if "minimize this window" in t or "minimize this" in t:
        return {"action": "minimize_current"}
    if re.search(r"minimize\s+(this|current|it|now)(\s+window)?$", t) or t.strip() == "minimize":
        return {"action": "minimize_current", "target": ""}

    m = re.search(r"minimize\s+(?:the\s+)?(?:(\w+)\s+)?(\w+)(?:\s+window)?$", t)
    if m and "all" not in t:
        ord_word, name = m.group(1), m.group(2)
        if name in APP_MAP or (name and name not in ("window", "this", "it", "all", "current", "now")):
            num = 1
            if ord_word:
                num = ORDINALS.get(ord_word) or (int(ord_word) if ord_word.isdigit() else 1)
            return {"action": "minimize_window_by_name", "target": name, "occurrence": num}

    if re.search(r"maximize\s+(this|current|it|now)(\s+window)?$", t) or t.strip() == "maximize":
        return {"action": "maximize_current", "target": ""}

    m = re.search(r"maximize\s+(?:the\s+)?(?:(\w+)\s+)?(\w+)(?:\s+window)?$", t)
    if m and "all" not in t:
        ord_word, name = m.group(1), m.group(2)
        if name in APP_MAP or (name and name not in ("window", "this", "it", "all", "current", "now")):
            num = 1
            if ord_word:
                num = ORDINALS.get(ord_word) or (int(ord_word) if ord_word.isdigit() else 1)
            return {"action": "maximize_window_by_name", "target": name, "occurrence": num}

    if re.search(r"(restore|un-?maximize|unmaximize)\s+(this|current|it|now)(\s+window)?$", t) \
            or t.strip() in ("restore", "unmaximize", "un-maximize"):
        return {"action": "restore_current", "target": ""}

    m = re.search(r"(?:restore|bring back|unminimize|open minimized)\s+(?:the\s+)?(?:(\w+)\s+)?(\w+)", t)
    if m:
        ord_word, name = m.group(1), m.group(2)
        if name and name not in ("all", "windows"):
            num = 1
            if ord_word:
                num = ORDINALS.get(ord_word) or (int(ord_word) if ord_word.isdigit() else 1)
            return {"action": "restore_window", "target": name, "occurrence": num}

    m = re.search(r"(?:switch to|focus|activate|bring up)\s+(?:the\s+)?(?:(\w+)\s+)?(\w+)(?:\s+window)?", t)
    if m and "tab" not in t:
        ord_word, name = m.group(1), m.group(2)
        if name in APP_MAP:
            num = 1
            if ord_word:
                num = ORDINALS.get(ord_word) or (int(ord_word) if ord_word.isdigit() else 1)
            return {"action": "focus_window", "target": name, "occurrence": num}

    # ---- Open app (must START with an open verb) ----
    for pat in [r"^(?:open|launch|start|run|execute)\s+(.+)$"]:
        m = re.match(pat, t)
        if m:
            app = m.group(1).strip().rstrip(".")
            if any(kw in app for kw in [" in ", " for ", " about ", " on ", " of ", " with "]):
                break
            if re.match(r"tab\s+\w+", app) or re.match(r"(?:the\s+)?\w+\s+tab$", app):
                continue
            if "account" in app and "chrome" in app:
                continue
            # URL typed as "open somesite.com"
            if is_url(app):
                return {"action": "open_url", "target": app}
            if app in APP_MAP:
                return wrap({"action": "open_app", "target": app})
            if len(app.split()) <= 3:
                for key in APP_MAP:
                    if key == app or (len(key) > 3 and (key in app or app in key)):
                        return wrap({"action": "open_app", "target": key})
            if len(app.split()) <= 2:
                return wrap({"action": "open_app", "target": app})

    # "search cats in chrome"
    m_search_in = re.match(r"^(?:search|find|look up|google)\s+(.+?)\s+in\s+(.+)$", t)
    if m_search_in:
        s_query = m_search_in.group(1).strip()
        s_where = m_search_in.group(2).strip()
        if s_where in APP_MAP:
            open_app(s_where)
            time.sleep(1.5)
            return wrap({"action": "search", "target": s_query})
        return wrap({"action": "search", "target": s_query})

    if t in APP_MAP:
        return {"action": "open_app", "target": t}

    # ---- Play / Pause ----
    if re.search(r"\b(play|pause|resume|stop playing)\b", t):
        return {"action": "play_pause", "target": ""}

    # ---- Volume ----
    if re.search(r"(volume up|increase volume|louder|raise (?:the )?volume)", t):
        amount = parse_number_words(t) or 5
        return {"action": "volume_up", "target": str(amount)}
    if re.search(r"(volume down|decrease volume|quieter|softer|lower (?:the )?volume)", t):
        amount = parse_number_words(t) or 5
        return {"action": "volume_down", "target": str(amount)}
    if re.search(r"(mute|unmute)", t):
        return {"action": "mute", "target": ""}

    # ---- Browser back (BEFORE media rewind) ----
    if _RE_GO_BACK.search(t):
        return {"action": "go_back", "target": ""}

    if _RE_BACKWARD.search(t):
        secs = parse_number_words(t) or 10
        return {"action": "backward", "target": str(secs)}

    if _RE_FORWARD.search(t):
        secs = parse_number_words(t) or 10
        return {"action": "forward", "target": str(secs)}

    # ---- Fullscreen ----
    if re.search(r"(full.?screen|enter full|exit full)", t):
        return {"action": "fullscreen", "target": ""}

    # ---- Scroll (must say scroll + direction) ----
    scroll_match = re.search(r"\bscroll(?:ing)?\s+(up|down|top|bottom)\b", t)
    if scroll_match:
        direction = scroll_match.group(1)
        scroll_amount = 10
        n = parse_number_words(t)
        if n and n < 100:
            scroll_amount = n * 2
        if re.search(r"\b(a lot|much|way|lot|lots)\b", t):
            scroll_amount = 40
        elif re.search(r"\b(little|bit|slightly|small|tiny)\b", t):
            scroll_amount = 4
        elif re.search(r"\b(halfway|half way|half)\b", t):
            scroll_amount = 15
        return {"action": "scroll_up" if direction in ("up", "top") else "scroll_down",
                "target": str(scroll_amount)}

    if re.search(r"\bpage\s+down\b", t) and "search" not in t and "find" not in t:
        return {"action": "scroll_down", "target": "15"}
    if re.search(r"\bpage\s+up\b", t) and "search" not in t and "find" not in t:
        return {"action": "scroll_up", "target": "15"}
    if re.search(r"scroll\s+to\s+(?:the\s+)?top", t):
        return {"action": "scroll_up", "target": "100"}
    if re.search(r"scroll\s+to\s+(?:the\s+)?bottom", t):
        return {"action": "scroll_down", "target": "100"}
    if re.search(r"\bscroll\b", t) and not re.search(r"(up|down|top|bottom|left|right)", t):
        return {"action": "ask_scroll_direction", "target": ""}

    # ---- MOUSE MOVEMENT (tolerates 'mouse'/'cursor' words inside) ----
    move_match = re.search(
        r"\b(?:move|go|cursor|mouse|come|shift|slide|drag|bring)\s+"
        r"(?:(?:the\s+|your\s+|this\s+)?(?:mouse|cursor|pointer)\s+)?"
        r"(?:(?:to|towards?|onto)\s+)?"
        r"(?:the\s+)?"
        r"(up right|upper right|up left|upper left|"
        r"down right|lower right|down left|lower left|"
        r"right|left|up|down)"
        r"(?:\s+(?:by\s+)?(\d+)(?:\s*(?:px|pixels?))?)?", t)
    if move_match:
        direction = move_match.group(1)
        pixels = move_match.group(2)

        if pixels:
            amount = int(pixels)
        else:
            amount = 200
            if re.search(r"\b(half|halfway|half\s*way)\b", t):
                amount = 100
            elif re.search(r"\b(quarter|little bit|tiny bit|small bit)\b", t):
                amount = 50
            elif re.search(r"\b(a lot|much more|way more|very far|really far|far)\b", t):
                amount = 500
            elif re.search(r"\b(double|twice|two times)\b", t):
                amount = 400
            elif re.search(r"\b(little|bit|slightly|small|tiny|short)\b", t):
                amount = 80
            elif re.search(r"\b(lot|much|more|way|big)\b", t):
                amount = 400
            n = parse_number_words(t)
            if n and n > 20:
                amount = n

        return {"action": "move_mouse", "target": direction, "amount": amount}

    # ---- Double / Right / regular click-here ----
    if re.search(r"\bdouble\s*click\s+(here|now|there|this)?\b", t):
        return {"action": "double_click_here", "target": ""}
    if t.strip() in ("double click", "doubleclick", "double tap"):
        return {"action": "double_click_here", "target": ""}

    if re.search(r"\bright\s*click\s+(here|now|there|this)?\b", t):
        return {"action": "right_click_here", "target": ""}
    if t.strip() in ("right click", "rightclick"):
        return {"action": "right_click_here", "target": ""}

    if re.search(r"\b(click|tap|press)\s+(here|now|there|this|at cursor|at mouse|cursor|mouse)\b", t):
        return {"action": "click_here", "target": ""}
    if t.strip() in ("click", "tap", "press", "click here", "tap here", "do it"):
        return {"action": "click_here", "target": ""}

    # ---- Grid selection ----
    grid_match = re.search(
        r"(?:click|open|select|play|tap)?\s*(?:on\s+)?(?:the\s+)?"
        r"(\w+)\s+(video|link|button|image|item|result|option|song|thumbnail)"
        r"(?:\s+in\s+(?:the\s+)?(\w+)\s+row)?", t)
    if grid_match:
        col_word = grid_match.group(1)
        item_type = grid_match.group(2)
        row_word = grid_match.group(3)
        col_num = ORDINALS.get(col_word) or parse_number_words(col_word)
        row_num = ORDINALS.get(row_word) if row_word else None
        if col_num:
            return {"action": "grid_click", "target": item_type,
                    "col": col_num, "row": row_num}

    # ---- Image click ----
    if is_image_query(t):
        desc = extract_image_description(t)
        if desc:
            return {"action": "click_image", "target": desc}

    # ---- Key hold ----
    hold_match = re.search(
        r"hold\s+(?:down\s+)?(?:the\s+)?(.+?)(?:\s+for\s+(\d+)\s*(?:seconds?|secs?)?)?$", t)
    if hold_match:
        key = hold_match.group(1).strip()
        duration = int(hold_match.group(2)) if hold_match.group(2) else 2
        return {"action": "hold_key", "target": key, "duration": duration}

    # ---- Press N times ----
    repeat_match = re.search(
        r"(?:press|hit|tap|push)\s+(?:the\s+)?(.+?)\s+(\d+)\s+times?", t)
    if repeat_match:
        combo = repeat_match.group(1).strip()
        times = int(repeat_match.group(2))
        if not re.search(r"\b(on|button|word|text|link)\b", combo):
            return {"action": "press_key_repeat", "target": combo, "times": times}

    # ---- Press (multi-word aware) ----
    m = re.match(r"(?:press|hit|tap)\s+(?:the\s+)?(.+?)(?:\s+key)?$", t)
    if m:
        key_phrase = m.group(1).strip()
        try:
            keys_ok = pyautogui.KEYBOARD_KEYS
        except Exception:
            keys_ok = []
        if (key_phrase in _KEY_ALIASES or
                key_phrase.replace(" ", "") in keys_ok or
                "+" in key_phrase):
            return wrap({"action": "press_key", "target": key_phrase})

    key_match = re.search(
        r"(?:press|hit|tap|push|type)\s+(?:the\s+)?(?:key\s+)?(.+)", t)
    if key_match:
        combo = key_match.group(1).strip().rstrip(".")
        if not re.search(r"\b(on|button|word|text|link|icon|image)\b", combo):
            test_keys = parse_key_combo(combo)
            is_valid_key = False
            for k in test_keys:
                k_lower = k.lower()
                if (k_lower in KEY_NAME_MAP or k_lower in KEY_NAME_MAP.values() or
                        k_lower in MODIFIER_MAP or k_lower in MODIFIER_MAP.values() or
                        (len(k_lower) == 1 and (k_lower.isalpha() or k_lower.isdigit())) or
                        re.match(r"f\d+", k_lower)):
                    is_valid_key = True
                    break
            if is_valid_key:
                return {"action": "press_key_combo", "target": combo}

    bare_key = t.strip()
    if bare_key in KEY_NAME_MAP and bare_key != "tab":
        return {"action": "press_key_combo", "target": bare_key}
    if re.match(r"^f\d+$", bare_key):
        return {"action": "press_key_combo", "target": bare_key}

    # ---- Click (with occurrence) ----
    for pat in [r"(?:click|tap|press|hit)\s+(?:on\s+)?(?:the\s+)?(?:(\w+)\s+)?(.+)",
                r"(?:navigate to)\s+(.+)"]:
        m = re.match(pat, t)
        if m:
            groups = m.groups()
            if len(groups) == 2 and groups[0] and groups[0] in ORDINALS:
                occurrence = ORDINALS[groups[0]]
                target = (groups[1] or "").strip().rstrip(".")
            else:
                occurrence = 1
                if len(groups) == 2 and groups[0]:
                    target = (groups[0] + " " + groups[1]).strip().rstrip(".")
                else:
                    target = (groups[-1] or "").strip().rstrip(".")
            if target:
                return {"action": "click_word", "target": target, "occurrence": occurrence}

    # ---- Select ----
    m = re.match(r"(?:select|highlight|choose)\s+(?:the\s+)?(.+)", t)
    if m:
        target = m.group(1).strip().rstrip(".")
        return {"action": "select_word", "target": target}

    # ---- Type ----
    m = re.match(r"(?:type|write|enter text)\s+(.+)", t)
    if m:
        return {"action": "type_text", "target": m.group(1).strip()}

    # ---- Search ----
    m = re.match(r"(?:search|find|look\s+up)\s+(?:for\s+)?(.+?)\s+on\s+youtube", t)
    if m:
        return {"action": "search_youtube", "target": m.group(1).strip()}

    m = re.match(r"(?:search|find)\s+youtube\s+for\s+(.+)", t)
    if m:
        return {"action": "search_youtube", "target": m.group(1).strip()}

    m = re.match(r"google\s+(?:for\s+)?(.+)", t)
    if m:
        return {"action": "google_search", "target": m.group(1).strip()}

    m = re.match(r"(?:search|find|look\s+up)\s+(?:for\s+)?(.+)", t)
    if m:
        query = m.group(1).strip()
        if not re.match(r"tab\s+\d", query):
            return {"action": "smart_search", "target": query}

    # ---- Window / misc ----
    if "close" in t and ("this" in t or "window" in t):
        return {"action": "close_app", "target": ""}
    if "minimize" in t:     return {"action": "minimize_current", "target": ""}
    if "maximize" in t:     return {"action": "maximize_current", "target": ""}
    if "screenshot" in t:   return {"action": "screenshot", "target": ""}
    if "new tab" in t:      return {"action": "new_tab", "target": ""}

    m = re.match(r"(?:press|hit|tap)\s+(?:the\s+)?(.+?)(?:\s+key)?$", t)
    if m:
        key_phrase = m.group(1).strip()
        try:
            keys_ok = pyautogui.KEYBOARD_KEYS
        except Exception:
            keys_ok = []
        if (key_phrase in _KEY_ALIASES or
                key_phrase.replace(" ", "") in keys_ok or
                "+" in key_phrase):
            return wrap({"action": "press_key", "target": key_phrase})

    if t in ("enter", "press enter", "return", "confirm", "ok", "okay", "submit"):
        return {"action": "press_key_combo", "target": "enter"}
    if t in ("escape", "esc", "press escape", "cancel", "dismiss"):
        return {"action": "press_key_combo", "target": "escape"}
    if t == "tab" or "press tab" in t:
        return {"action": "press_key_combo", "target": "tab"}
    if t in ("page down", "pagedown"):
        return {"action": "press_key", "target": "pagedown"}
    if t in ("page up", "pageup"):
        return {"action": "press_key", "target": "pageup"}

    # =================================================
    #  FALLBACK - the ONLY place the LLM is allowed in,
    #  and never for questions / chit-chat.
    # =================================================
    if looks_like_question(text) or looks_like_question(t):
        print(f"  [Question] not a screen command: \"{text}\"")
        return {"action": "unknown", "target": text}

    if use_fallback and ENABLE_LLM and ollama_ok():
        translated = ollama_translate_command(text)

        if translated and translated.strip() != text.lower().strip():
            bad_translations = {"unknown", "scroll", "move", "click",
                                "press", "open", "nothing", "error",
                                "none", "", " "}
            if translated.strip() in bad_translations:
                print(f"  [Fallback] rejected unclear translation '{translated}'")
            else:
                print(f"  [Fallback] Re-parsing: \"{translated}\"")
                result = parse_command(translated, use_fallback=False)
                if result.get("action") not in ("click_word",):
                    return result
                if result.get("target") and len(result.get("target", "")) > 2:
                    return result

        kw = extract_keyword(t)
        if kw and kw != text.lower().strip() and len(kw) > 1:
            direction_words = ["up", "down", "left", "right", "top", "bottom",
                               "scroll", "move", "page"]
            if kw.lower() not in direction_words:
                if kw in APP_MAP:
                    return {"action": "open_app", "target": kw}
                return {"action": "click_word", "target": kw, "occurrence": 1}

    return safe_last_resort(t, text)

# =============================================================
#  Actions
# =============================================================
def open_url(url):
    speak(f"Opening {url}")
    import webbrowser
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)


def open_app(name):
    app = name.lower().strip()
    speak(f"Opening {name}")

    if app in APP_MAP:
        method, target = APP_MAP[app]
        try:
            if method == "startfile":
                os.startfile(target)
            elif method == "run":
                subprocess.Popen([target])
            elif method == "start":
                subprocess.Popen(f'start "" "{target}"', shell=True)
            elif method == "url":
                import webbrowser
                webbrowser.open(target)
                speak(f"{name} opened.")
                return
            time.sleep(0.5)
            speak(f"{name} opened.")
            return
        except Exception as e:
            print(f"  [!] Direct launch failed: {e}")

    speak(f"Searching for {name}")
    pyautogui.press("win")
    time.sleep(1.0)
    pyautogui.typewrite(name, interval=0.03)
    time.sleep(1.5)
    pyautogui.press("enter")
    time.sleep(0.5)
    speak("Done.")


def open_chrome_account(num=None):
    speak("Opening Chrome")
    try:
        subprocess.Popen('start "" "chrome"', shell=True)
    except Exception as e:
        print(f"  [!] Chrome launch failed: {e}")
        return

    if num is None:
        return
    try:
        num = int(num)
    except Exception:
        num = 1

    speak(f"Selecting account {num}")
    _ocr_cache["words"] = None
    found = None
    for _ in range(10):
        time.sleep(0.5)
        words = ocr_screen(force=True)
        picker_markers = ["choose", "profile", "who", "using", "account",
                          "select", "gmail.com", "@"]
        has_picker = any(any(mk in w["text"] for mk in picker_markers) for w in words)
        if has_picker:
            found = words
            break

    if not found:
        print("  [Chrome] No account picker detected.")
        return

    found.sort(key=lambda w: (w["y"], w["x"]))
    emails = [w for w in found if "@" in w["raw"] or "gmail" in w["text"]]
    if emails:
        emails.sort(key=lambda w: w["x"])
        if num <= len(emails):
            pick = emails[num - 1]
            print(f"  [Chrome] Clicking account #{num}: {pick['raw']}")
            smart_click(pick["x"], pick["y"])
            return

    screen_h = pyautogui.size()[1]
    center_band = [w for w in found if screen_h * 0.25 < w["y"] < screen_h * 0.75]
    center_band.sort(key=lambda w: w["x"])
    if num <= len(center_band):
        pick = center_band[num - 1]
        smart_click(pick["x"], pick["y"])
    else:
        speak("Could not find that account")


def click_tab_by_name(name):
    speak(f"Switching to {name} tab")
    words = ocr_screen(force=True)
    if not words:
        speak("Can't read screen")
        return

    screen_w, screen_h = pyautogui.size()
    tab_strip = [w for w in words if w["y"] < 90]
    if not tab_strip:
        tab_strip = [w for w in words if w["y"] < screen_h * 0.1]

    name_low = name.lower().strip()
    name_parts = name_low.split()

    best = None
    best_score = 0

    for w in tab_strip:
        if w["text"] == name_low:
            score = 1.0
        elif name_low in w["text"] or w["text"] in name_low:
            score = SequenceMatcher(None, name_low, w["text"]).ratio()
        else:
            score = SequenceMatcher(None, name_low, w["text"]).ratio()
        if score > best_score and score > 0.55:
            best_score = score
            best = w

    if len(name_parts) > 1:
        tab_strip_sorted = sorted(tab_strip, key=lambda w: w["x"])
        texts = [w["text"] for w in tab_strip_sorted]
        for i in range(len(texts) - len(name_parts) + 1):
            chunk = " ".join(texts[i:i + len(name_parts)])
            r = SequenceMatcher(None, name_low, chunk).ratio()
            if r > best_score and r > 0.7:
                mid = tab_strip_sorted[i + len(name_parts) // 2]
                best_score = r
                best = mid

    if best:
        print(f"  [Tab] Found '{best['raw']}' score={best_score:.2f}")
        reliable_click(best["x"], best["y"])
    else:
        c = find_on_screen(name, prefer_region="top")
        if c:
            reliable_click(c[0], c[1])
        else:
            speak(f"No tab named {name}")


def click_word(word, occurrence=1):
    speak(f"Clicking {word}")
    coords = find_on_screen(word, occurrence=occurrence, prefer_region="center")
    if coords:
        smart_click(*coords)
        return
    hw_scroll(-5)
    time.sleep(0.6)
    coords = find_on_screen(word, occurrence=occurrence, force_ocr=True)
    if coords:
        smart_click(*coords)
        return
    hw_scroll(5)
    time.sleep(0.4)
    if ollama_ok():
        kw = extract_keyword(f"click {word}")
        if kw and kw != word:
            c2 = find_on_screen(kw, force_ocr=True)
            if c2:
                smart_click(*c2)
                return
    speak(f"I can't find {word} on screen")


def click_image(description):
    if not vision_model_ok():
        speak("Vision model not available. Trying text search instead.")
        click_word(description)
        return

    speak(f"Analyzing screen for {description}")
    coords = find_image_on_screen(description)
    if coords:
        speak("Found it. Clicking.")
        smart_click(coords[0], coords[1])
        return

    speak("Not visible. Scrolling to find it.")
    hw_scroll(-5)
    time.sleep(1.0)
    coords = find_image_on_screen(description)
    if coords:
        speak("Found it after scrolling.")
        smart_click(coords[0], coords[1])
        return

    hw_scroll(5)
    speak(f"Image not found. Trying to find the text {description}.")
    click_word(description)


def select_word(word):
    speak(f"Selecting {word}")
    coords = find_on_screen(word)
    if coords:
        reliable_double_click(coords[0], coords[1])
        speak("Selected.")
    else:
        speak(f"Can't find {word}.")


def grid_click(item_type, col, row=None):
    label = f"item {col} ({item_type})"
    if row:
        label += f" in row {row}"
    speak(f"Looking for {label}")
    coords = find_grid_element(item_type, col, row)
    if coords:
        speak("Found it. Clicking.")
        smart_click(coords[0], coords[1])
    else:
        speak(f"Couldn't identify the {label}.")


def type_text(text):
    speak("Typing")
    time.sleep(0.3)
    try:
        pyautogui.write(text, interval=0.03)
    except Exception:
        try:
            pyautogui.typewrite(text, interval=0.03)
        except Exception as e:
            print(f"  [!] Type error: {e}")


def do_search(query):
    speak(f"Searching for {query}")
    import webbrowser
    webbrowser.open(f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}")


def smart_open(target):
    target = target.strip().rstrip(".").lower()
    if not target:
        speak("Open what?")
        return

    if target in APP_MAP:
        open_app(target)
        return
    for key in APP_MAP:
        if key == target or (len(target) > 3 and target in key) or (len(key) > 3 and key in target):
            open_app(key)
            return

    if is_url(target):
        open_url(target)
        return

    ctx = get_context()
    print(f"  [Context] Active: {ctx}")

    if ctx == "browser":
        speak(f"Opening {target} in browser")
        pyautogui.hotkey("ctrl", "t")
        time.sleep(0.4)
        pyautogui.typewrite(target, interval=0.02)
        time.sleep(0.2)
        pyautogui.press("enter")
        return

    if ctx == "explorer":
        speak(f"Searching for {target} in Explorer")
        pyautogui.hotkey("ctrl", "e")
        time.sleep(0.4)
        pyautogui.typewrite(target, interval=0.02)
        time.sleep(0.2)
        pyautogui.press("enter")
        return

    if ctx == "editor":
        speak(f"Opening file {target}")
        pyautogui.hotkey("ctrl", "o")
        time.sleep(0.6)
        pyautogui.typewrite(target, interval=0.02)
        return

    speak(f"Searching Windows for {target}")
    pyautogui.press("win")
    time.sleep(0.8)
    pyautogui.typewrite(target, interval=0.03)
    time.sleep(1.2)
    pyautogui.press("enter")


def smart_search(query):
    query = query.strip().rstrip(".")
    if not query:
        speak("Search for what?")
        return

    ctx = get_context()
    print(f"  [Context] Active: {ctx}")

    if ctx == "browser":
        speak(f"Searching for {query}")
        pyautogui.hotkey("ctrl", "l")
        time.sleep(0.3)
        pyautogui.typewrite(query, interval=0.02)
        time.sleep(0.2)
        pyautogui.press("enter")
        return

    if ctx == "explorer":
        speak(f"Searching files for {query}")
        pyautogui.hotkey("ctrl", "e")
        time.sleep(0.4)
        pyautogui.typewrite(query, interval=0.02)
        time.sleep(0.2)
        pyautogui.press("enter")
        return

    speak(f"Googling {query}")
    import webbrowser
    webbrowser.open(f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}")


def smart_search_youtube(query):
    query = query.strip().rstrip(".")
    speak(f"Searching YouTube for {query}")
    import webbrowser
    webbrowser.open(f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}")


def show_translations():
    if not _translation_cache:
        speak("Nothing learned yet")
        return
    print("\n  === Learned Translations ===")
    for user, cmd in _translation_cache.items():
        print(f"  \"{user}\" -> \"{cmd}\"")
    print()
    speak(f"I've learned {len(_translation_cache)} translations")


# ---- Media ----
def play_pause():
    speak("Play pause")
    press_vk(VK_MEDIA_PLAY)


def volume_up(amount=5):
    try:
        amount = int(amount)
    except Exception:
        amount = 5
    speak(f"Volume up {amount}")
    press_vk(VK_VOLUME_UP, times=amount, delay=0.06)


def volume_down(amount=5):
    try:
        amount = int(amount)
    except Exception:
        amount = 5
    speak(f"Volume down {amount}")
    press_vk(VK_VOLUME_DOWN, times=amount, delay=0.06)


def mute_toggle():
    speak("Mute toggle")
    press_vk(VK_VOLUME_MUTE)


def media_next():
    speak("Next track")
    press_vk(VK_MEDIA_NEXT)


def media_prev():
    speak("Previous track")
    press_vk(VK_MEDIA_PREV)


def forward(secs=10):
    speak(f"Forward {secs} seconds")
    for _ in range(max(int(secs) // 5, 1)):
        pyautogui.press("right")
        time.sleep(0.05)


def backward(secs=10):
    speak(f"Backward {secs} seconds")
    for _ in range(max(int(secs) // 5, 1)):
        pyautogui.press("left")
        time.sleep(0.05)


def fullscreen():
    ctx = get_context()
    cls, title, proc = get_foreground_window_info()

    if ctx == "browser" and "youtube" in title:
        speak("Fullscreen")
        pyautogui.press("f")
        return
    if "vlc" in proc:
        speak("Fullscreen")
        pyautogui.press("f")
        return
    media_procs = ("vlc.exe", "mpv.exe", "mpc-hc.exe", "mpc-hc64.exe",
                   "potplayer.exe", "potplayermini.exe", "potplayermini64.exe")
    if any(mp in proc for mp in media_procs):
        speak("Fullscreen")
        pyautogui.press("f")
        return
    speak("Fullscreen")
    pyautogui.press("f11")


# ---- Scroll ----
def scroll_up(amount=5):
    speak("Scrolling up")
    hw_scroll(int(amount) * 3)


def scroll_down(amount=5):
    speak("Scrolling down")
    hw_scroll(-int(amount) * 3)


def ask_scroll_direction():
    speak("Scroll up or down?")


def handle_unknown(text=""):
    print(f"  [Unknown] Could not understand: '{text}'")
    speak("I didn't catch that. Please repeat.")


def go_back():
    speak("Back")
    pyautogui.hotkey("alt", "left")


def close_win():
    speak("Closing")
    pyautogui.hotkey("alt", "f4")


def close_tab():
    speak("Closed tab")
    pyautogui.hotkey("ctrl", "w")


def new_tab():
    speak("New tab")
    pyautogui.hotkey("ctrl", "t")


def press_key(key_name):
    key_name = key_name.strip()
    if "+" in key_name:
        parts = [normalize_key_name(p) for p in key_name.split("+")]
        speak(f"Pressing {'+'.join(parts)}")
        pyautogui.hotkey(*parts)
    else:
        normalized = normalize_key_name(key_name)
        speak(f"Key {normalized}")
        pyautogui.press(normalized)


def press_key_combo(combo_text):
    keys = parse_key_combo(combo_text)
    if not keys:
        speak("No key recognized")
        return
    if len(keys) == 1:
        speak(f"Pressing {keys[0]}")
        try:
            pyautogui.press(keys[0])
        except Exception as e:
            print(f"  [!] Key press error: {e}")
            speak(f"Cannot press {keys[0]}")
    else:
        combo_name = " + ".join(keys)
        speak(f"Pressing {combo_name}")
        try:
            pyautogui.hotkey(*keys)
        except Exception as e:
            print(f"  [!] Hotkey error: {e}")
            speak(f"Cannot press {combo_name}")
    print(f"  [Key] Pressed: {keys}")


def press_key_repeat(combo_text, times=1):
    keys = parse_key_combo(combo_text)
    if not keys:
        speak("No key recognized")
        return
    combo_name = " + ".join(keys)
    speak(f"Pressing {combo_name} {times} times")
    for i in range(int(times)):
        try:
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)
            time.sleep(0.1)
        except Exception as e:
            print(f"  [!] Key press error on iteration {i+1}: {e}")
            break
    print(f"  [Key] Pressed {keys} x{times}")


def hold_key(key_name, duration=2):
    key = resolve_key_name(key_name)
    speak(f"Holding {key}")
    try:
        pyautogui.keyDown(key)
        time.sleep(duration)
        pyautogui.keyUp(key)
    except Exception as e:
        print(f"  [!] Hold key error: {e}")
        try:
            pyautogui.keyUp(key)
        except Exception:
            pass


def screenshot():
    ts = time.strftime("%Y%m%d_%H%M%S")
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"screenshot_{ts}.png")
    try:
        pyautogui.screenshot().save(p)
        speak("Screenshot saved.")
    except Exception as e:
        print(f"  [!] Screenshot error: {e}")
        speak("Could not take screenshot.")


def execute(intent):
    action = intent.get("action", "")
    target = intent.get("target", "")
    repeat = intent.get("repeat", 1)

    print(f"  [EXECUTE] action='{action}' target='{target}' repeat={repeat}")

    if action == "execute_plan":
        execute_plan(intent.get("steps", []), target)
        return

    action_map = {
        "open_app":          lambda: open_app(target),
        "smart_open":        lambda: smart_open(target),
        "open_url":          lambda: open_url(target),
        "chrome_account":    lambda: open_chrome_account(target),
        "click_tab":         lambda: click_tab_by_name(target),
        "switch_tab_number": lambda: switch_tab_number(target),
        "next_tab":          next_tab,
        "prev_tab":          prev_tab,
        "close_nth_tab":     lambda: close_nth_tab(target),
        "minimize_current":      minimize_current,
        "maximize_current":      maximize_current,
        "restore_current":       restore_current,
        "close_window_by_name":  lambda: close_window_by_name(target, intent.get("occurrence", 1)),
        "minimize_window_by_name": lambda: minimize_window_by_name(target, intent.get("occurrence", 1)),
        "maximize_window_by_name": lambda: maximize_window_by_name(target, intent.get("occurrence", 1)),
        "restore_window":    lambda: restore_window_by_name(target, intent.get("occurrence", 1)),
        "minimize_all":          minimize_all_windows,
        "maximize_all":          maximize_all_windows,
        "restore_all_minimized": restore_all_minimized,
        "close_all_minimized":   close_all_minimized,
        "focus_window":      lambda: click_taskbar_icon(target, intent.get("occurrence", 1)),
        "list_windows":      list_open_windows,
        "move_mouse":        lambda: move_mouse(target, intent.get("amount", 200)),
        "click_here":        click_here,
        "double_click_here": double_click_here,
        "right_click_here":  right_click_here,
        "click_word":        lambda: click_word(target, intent.get("occurrence", 1)),
        "click_image":       lambda: click_image(target),
        "select_word":       lambda: select_word(target),
        "grid_click":        lambda: grid_click(target, intent.get("col", 1), intent.get("row")),
        "type_text":         lambda: type_text(target),
        "search":            lambda: do_search(target),
        "smart_search":      lambda: smart_search(target),
        "google_search":     lambda: do_search(target),
        "search_youtube":    lambda: smart_search_youtube(target),
        "play_pause":        play_pause,
        "volume_up":         lambda: volume_up(int(target) if str(target).isdigit() else 5),
        "volume_down":       lambda: volume_down(int(target) if str(target).isdigit() else 5),
        "mute":              mute_toggle,
        "media_next":        media_next,
        "media_prev":        media_prev,
        "forward":           lambda: forward(int(target) if str(target).isdigit() else 10),
        "backward":          lambda: backward(int(target) if str(target).isdigit() else 10),
        "fullscreen":        fullscreen,
        "scroll_up":         lambda: scroll_up(int(target) if str(target).isdigit() else 5),
        "scroll_down":       lambda: scroll_down(int(target) if str(target).isdigit() else 5),
        "ask_scroll_direction": ask_scroll_direction,
        "unknown":           lambda: handle_unknown(target),
        "go_back":           go_back,
        "close_app":         close_win,
        "close_tab":         close_tab,
        "new_tab":           new_tab,
        "press_key":         lambda: press_key(target),
        "press_key_combo":   lambda: press_key_combo(target),
        "press_key_repeat":  lambda: press_key_repeat(target, intent.get("times", 1)),
        "hold_key":          lambda: hold_key(target, intent.get("duration", 2)),
        "screenshot":        screenshot,
        "show_translations": show_translations,
        "continuous_scroll": lambda: continuous_scroll(target, intent.get("speed", "normal")),
        "continuous_move":   lambda: continuous_move(target, intent.get("speed", "normal")),
        "stop_continuous":   handle_stop,
    }

    fn = action_map.get(action)
    if fn:
        for _ in range(max(1, int(repeat))):
            try:
                fn()
            except Exception as e:
                print(f"  [!] Action '{action}' failed: {e}")
                speak("That didn't work")
            if repeat > 1:
                time.sleep(0.1)
    else:
        print(f"  [!] Unknown action: '{action}'")
        speak("I didn't understand that.")

# =============================================================
#  Banner & Main
# =============================================================
BANNER = r"""
  ================================================
       ROLEX - Voice Screen Agent  (v3.1 fixed)
  ================================================

   WAKE:     Say "Rolex" (while sleeping, wakes it up)
             "Rolex [command]" (wakes + runs command)

   SLEEP:    "Rolex sleep" / "Sleep rolex" / "Rolex goodnight"
   EXIT:     "Rolex exit" / "Exit rolex" / "Rolex goodbye"
             (MUST say Rolex - prevents accidents!)

   APPS:     "Open Chrome / VLC / Explorer / YouTube"
   MOVE:     "Go right" / "Go up a little" / "Move left 300"
             "Move the mouse to the right" / "Slide cursor left"
             "Go to the right" / "Drag pointer down"
   CLICK:    "Click here" / "Double click" / "Right click"
             "Click on [word]" / "Click the 2nd [word]"
   SCROLL:   "Scroll up/down" "Scroll down a lot" "Scroll to top"
   MEDIA:    "Play" "Pause" "Volume up/down" "Mute" "Fullscreen"
   TABS:     "Go to tab 2" "Next tab" "Close tab" "Go to gmail tab"
   WINDOWS:  "Minimize / Maximize / Restore" (current or by name)
   TYPE:     "Type [text]"  "Search for [query]"
   KEYS:     "Press Ctrl C" "Press Alt F4" "Press Enter 5 times"
   MULTI:    "Open jersey in movies in file explorer"
             "Search for cats on youtube"
   STOP:     "Stop" / "That's enough" (during continuous actions)

   Run  'python rolex_agent.py --selftest'  to see how
   the parser handles your phrasing (no mic needed).
  ================================================
"""


def banner():
    os.system("cls" if os.name == "nt" else "clear")
    print(BANNER)


_SELFTEST_CASES = [
    "move right", "move left", "go up", "go down",
    "move the mouse to the right",
    "move mouse to the left",
    "slide cursor to the right",
    "drag pointer down",
    "move the cursor up a little",
    "move right 300",
    "move left by 150",
    "go to the right",
    "go to the bottom",
    "go down a lot",
    "go up half",
    "can you open chrome please",
    "please scroll down",
    "could you move right for me",
    "volume up", "turn up the volume", "turn it up",
    "make it louder", "increase the volume", "mute",
    "scroll down", "scroll up a little", "scroll to the bottom",
    "scroll", "play", "pause the video", "click on save",
    "click the third button", "press ctrl c", "press alt f4",
    "press enter 5 times", "type hello world",
    "search for cats on youtube", "minimize chrome",
    "close chrome", "shut down chrome", "turn off vlc",
    "go to tab 2", "open youtube", "go to github.com",
    "open google.com", "close notepad", "close spotify",
    "what time is it", "how are you",
    "go to sleep", "exit", "minimize this", "maximize this",
    "take a screenshot", "new tab", "next tab",
]


def run_selftest():
    print(BANNER)
    print("  === Parser self-test (local rules only, LLM disabled) ===\n")
    passed = unknown = 0
    for phrase in _SELFTEST_CASES:
        try:
            # force local-only so the test never touches the network/LLM
            intent = parse_command(phrase, use_fallback=False)
        except Exception as e:
            intent = {"action": "ERROR", "target": str(e)}
        action = intent.get("action", "?")
        if action == "unknown":
            unknown += 1
            mark = "?? "
        else:
            passed += 1
            mark = "ok "
        print(f"  {mark} {phrase:42s} -> {action}  {intent.get('target', '')}"
              f"{'  x' + str(intent.get('repeat', 1)) if intent.get('repeat', 1) > 1 else ''}")
    print(f"\n  Local rules handled: {passed}/{len(_SELFTEST_CASES)}"
          f"   (unknown: {unknown})")
    print("  Anything marked ?? would be the only ones that fall back to the LLM.\n")


def main():
    t_start = time.time()
    banner()
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.02

    # Ollama check runs in the BACKGROUND - waking must never wait for it
    if ENABLE_LLM:
        threading.Thread(target=ollama_ok, daemon=True).start()

    calibrate_noise_floor()

    try:
        load_translations()
    except Exception:
        pass

    if _REDIS_AVAILABLE:
        print("  [OK] Redis caching enabled")
    print(f"  [*] Startup: {time.time() - t_start:.1f}s - mic is armed")
    print()

    speak("Rolex ready.")

    active = False
    last_command_time = time.time()

    while True:
        try:
            # ================== SLEEP MODE ==================
            if not active:
                print("\n  [SLEEP] Waiting for wake word 'Rolex'...")
                text = listen(timeout=10)

                if not text:
                    continue

                if not has_wake(text):
                    print(f"  [Ignored - need wake word] \"{text}\"")
                    continue

                if is_exit(text):
                    speak("Goodbye")
                    try:
                        save_translations()
                    except Exception:
                        pass
                    return

                if is_sleep_command(text):
                    print("  [Info] Already sleeping")
                    continue

                cmd = strip_wake(text)
                active = True
                last_command_time = time.time()

                if cmd and len(cmd.strip()) >= 1:
                    print(f"  [Wake+Command] \"{cmd}\"")
                    try:
                        intent = parse_command(cmd)
                        if intent.get("action") != "unknown":
                            last_command_time = time.time()
                        execute(intent)
                    except Exception as e:
                        print(f"  [!] Error executing wake command: {e}")
                        speak("Error running that")
                    print("  [ACTIVE] Ready for more commands")
                else:
                    speak("Yes?")
                    print("  [WAKE] Now active")

            # ================== ACTIVE MODE ==================
            else:
                if _continuous_action.get("running"):
                    time.sleep(0.2)
                    continue
                print("\n  [ACTIVE] Listening for command...")
                text = listen(timeout=15)

                if not text:
                    if time.time() - last_command_time > get_auto_sleep_timeout():
                        active = False
                        speak("Going to sleep due to inactivity.")
                        print("  [SLEEP] Auto-sleep due to inactivity")
                    else:
                        remaining = int(get_auto_sleep_timeout() - (time.time() - last_command_time))
                        print(f"  [...] Silence. Still active. (Auto-sleep in {max(0, remaining)}s)")
                    continue

                if time.time() - last_command_time > get_auto_sleep_timeout():
                    active = False
                    speak("Going to sleep.")
                    print("  [SLEEP] Inactivity timeout expired")
                    continue

                # PRIORITY 1: stop continuous action (no wake word needed)
                try:
                    if _continuous_action.get("running") and is_stop_command(text):
                        stop_continuous_action()
                        speak("Stopped")
                        last_command_time = time.time()
                        continue
                except Exception:
                    pass

                # PRIORITY 2: EXIT (requires "rolex")
                if is_exit(text):
                    speak("Goodbye")
                    try:
                        stop_continuous_action()
                    except Exception:
                        pass
                    try:
                        save_translations()
                    except Exception:
                        pass
                    return

                # PRIORITY 3: SLEEP (requires "rolex")
                if is_sleep_command(text):
                    try:
                        stop_continuous_action()
                    except Exception:
                        pass
                    speak("Going to sleep.")
                    active = False
                    print("  [SLEEP] Going to sleep")
                    continue

                # PRIORITY 4: strip wake word if re-said
                if has_wake(text):
                    text = strip_wake(text)
                    if not text or len(text) < 2:
                        speak("Yes?")
                        last_command_time = time.time()
                        continue

                intent = parse_command(text)

                if intent.get("action") != "unknown":
                    last_command_time = time.time()

                execute(intent)

        except KeyboardInterrupt:
            print("\n  [Interrupt] Shutting down...")
            speak("Bye", wait=True)
            try:
                stop_continuous_action()
            except Exception:
                pass
            try:
                save_translations()
            except Exception:
                pass
            return

        except Exception as e:
            print(f"  [ERROR] {e}")
            import traceback
            traceback.print_exc()
            continue


def run_waketest():
    """No microphone needed: checks that wake-word detection works offline."""
    cases = [
        ("rolex", True), ("hey rolex", True), ("rolex open chrome", True),
        ("relax", True), ("relax open chrome", True), ("roles open chrome", True),
        ("rolaks", True), ("rowlex pause", True), ("relex", True),
        ("rolex volume up", True), ("rolex scroll down", True),
        ("hello", False), ("open chrome", False), ("chrome", False),
        ("scroll down", False), ("notes", False), ("rocky horror", False),
    ]
    ok = True
    print("\n  === Wake-word detection test ===\n")
    for phrase, expected in cases:
        got = has_wake(phrase)
        mark = "ok " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"  {mark}  {phrase:26s} -> wake={got} (expected {expected})")
        if got == expected and got:
            print(f"          stripped -> \"{strip_wake(phrase)}\"")
    print("\n  " + ("ALL PASS" if ok else "SOME FAILED"))


def run_listen_test():
    """Microphone test: listens ONCE and shows exactly what STT heard."""
    print("\n  === Mic / STT diagnostic ===")
    calibrate_noise_floor()
    print("\n  Say 'Rolex' (or a command) after the beep...")
    speak("Listening")
    text = listen(timeout=8)
    print(f"\n  Transcript : {text!r}")
    if text:
        print(f"  Wake word  : {has_wake(text)}")
        print(f"  Stripped   : {strip_wake(text)!r}")
        print(f"  Intent     : {parse_command(strip_wake(text), use_fallback=False)}")
    else:
        print("  NOTHING heard - check the [Mic] line above, your default")
        print("  input device, and that Windows sound input is not muted.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()
    elif "--waketest" in sys.argv:
        run_waketest()
    elif "--listen" in sys.argv:
        run_listen_test()
    else:
        main()
