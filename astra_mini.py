"""
 ===============================================
  ASTRA MINI v3 - Voice-Controlled Screen Agent
 ===============================================
"""

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

# Fix Windows console encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---- DPI FIX: MUST run before any screen operations ------------------------
try:
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
        print("  [*] DPI awareness: per-monitor")
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()        # Fallback
        print("  [*] DPI awareness: system")
except Exception as _e:
    print(f"  [!] DPI awareness failed: {_e}")

DEBUG_MODE = True  # Set to False in production

def debug_log(msg):
    if DEBUG_MODE:
        print(f"  [DEBUG] {msg}")


# ---- Reliable Mouse Click (SendInput - modern win32 API) --------------------
try:
    import ctypes
    _user32 = ctypes.windll.user32

    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP   = 0x0004
    INPUT_MOUSE          = 0

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx",          ctypes.c_long),
            ("dy",          ctypes.c_long),
            ("mouseData",   ctypes.c_ulong),
            ("dwFlags",     ctypes.c_ulong),
            ("time",        ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("_u",)
        _fields_ = [("type", ctypes.c_ulong), ("_u", INPUT_UNION)]

    def _send_click():
        down = INPUT(type=INPUT_MOUSE, _u=INPUT_UNION(mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_LEFTDOWN)))
        up   = INPUT(type=INPUT_MOUSE, _u=INPUT_UNION(mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_LEFTUP)))
        _user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        time.sleep(0.08)
        _user32.SendInput(1, ctypes.byref(up),   ctypes.sizeof(INPUT))

    def reliable_click(x, y):
        x, y = int(x), int(y)
        _user32.SetCursorPos(x, y)
        time.sleep(0.04)              # was 0.12
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

    print("  [*] Using SendInput mouse API")

except Exception as _e:
    print(f"  [!] win32 init failed ({_e}); using pyautogui fallback")

    def reliable_click(x, y):
        pyautogui.click(x=int(x), y=int(y))

    def reliable_double_click(x, y):
        pyautogui.doubleClick(x=int(x), y=int(y))


# ---- Window enumeration & management (for taskbar operations) --------------
import ctypes.wintypes as wintypes

EnumWindows = _user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.POINTER(ctypes.c_int))
GetWindowText = _user32.GetWindowTextW
GetWindowTextLength = _user32.GetWindowTextLengthW
IsWindowVisible = _user32.IsWindowVisible
IsIconic = _user32.IsIconic  # True if minimized
GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
ShowWindow = _user32.ShowWindow
PostMessage = _user32.PostMessageW
SetForegroundWindow = _user32.SetForegroundWindow

SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE  = 9
SW_SHOW     = 5
WM_CLOSE    = 0x0010

def get_process_name(hwnd):
    """Get the exe name of the process owning this window."""
    try:
        import psutil
        pid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return psutil.Process(pid.value).name().lower()
    except Exception:
        return ""

def enum_windows():
    """Return list of dicts: {hwnd, title, process, minimized} for visible windows."""
    results = []
    def _cb(hwnd, lparam):
        if not IsWindowVisible(hwnd):
            return True
        length = GetWindowTextLength(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        GetWindowText(hwnd, buf, length + 1)
        title = buf.value
        # Skip system shell windows
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
    """Find all windows matching an app name (chrome, vlc, notepad, etc)."""
    name = name.lower().strip()
    wins = enum_windows()
    matches = []
    # Map common names to process names
    proc_aliases = {
        "chrome": ["chrome.exe"],
        "edge": ["msedge.exe"],
        "firefox": ["firefox.exe"],
        "brave": ["brave.exe"],
        "vlc": ["vlc.exe"],
        "notepad": ["notepad.exe"],
        "explorer": ["explorer.exe"],
        "file explorer": ["explorer.exe"],
        "calculator": ["calculatorapp.exe", "calc.exe"],
        "spotify": ["spotify.exe"],
        "discord": ["discord.exe"],
        "word": ["winword.exe"],
        "excel": ["excel.exe"],
    }
    target_procs = proc_aliases.get(name, [name + ".exe", name])
    for w in wins:
        if any(tp in w["process"] for tp in target_procs):
            matches.append(w)
        elif name in w["title"].lower():
            matches.append(w)
    return matches

# ---- Taskbar / Window Management Actions ----------------------------------

def close_window_by_name(name, occurrence=1):
    """Close the Nth window matching name. 'all' closes all."""
    matches = find_windows_by_name(name)
    if not matches:
        speak(f"No {name} windows found")
        return
    
    if occurrence == "all" or str(occurrence).lower() == "all":
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
    
    target = matches[n - 1]
    speak(f"Closing {name} window {n}")
    PostMessage(target["hwnd"], WM_CLOSE, 0, 0)

def minimize_window_by_name(name, occurrence=1):
    matches = find_windows_by_name(name)
    if not matches:
        speak(f"No {name} windows found")
        return
    if occurrence == "all":
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

def close_all_minimized():
    """Close every minimized window."""
    wins = enum_windows()
    minimized = [w for w in wins if w["minimized"]]
    if not minimized:
        speak("No minimized windows")
        return
    speak(f"Closing {len(minimized)} minimized windows")
    for w in minimized:
        PostMessage(w["hwnd"], WM_CLOSE, 0, 0)
        time.sleep(0.08)

# ---- Enhanced Minimize / Maximize / Restore (current window) --------------

def minimize_current():
    """Minimize the currently focused window."""
    speak("Minimizing")
    try:
        hwnd = _user32.GetForegroundWindow()
        ShowWindow(hwnd, SW_MINIMIZE)
    except Exception:
        pyautogui.hotkey("win", "down")
        time.sleep(0.1)
        pyautogui.hotkey("win", "down")

def maximize_current():
    """Maximize the currently focused window."""
    speak("Maximizing")
    try:
        hwnd = _user32.GetForegroundWindow()
        ShowWindow(hwnd, SW_MAXIMIZE)
    except Exception:
        pyautogui.hotkey("win", "up")

def restore_current():
    """Restore the currently focused window (un-maximize)."""
    speak("Restoring window")
    try:
        hwnd = _user32.GetForegroundWindow()
        ShowWindow(hwnd, SW_RESTORE)
    except Exception:
        pyautogui.hotkey("win", "down")

def maximize_window_by_name(name, occurrence=1):
    """Maximize a specific window by app name."""
    matches = find_windows_by_name(name)
    if not matches:
        speak(f"No {name} windows found")
        return

    if occurrence == "all" or str(occurrence).lower() == "all":
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

def maximize_all_windows():
    """Maximize all open visible windows."""
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
    """Restore all minimized windows."""
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
    """Speak the list of open windows (useful for debugging)."""
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
    """Click a taskbar icon by hovering over the taskbar and matching."""
    # Simpler approach: just activate the window via API
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


# ---- Detect windows that need double-click (File Explorer, Desktop) --------
def get_foreground_class():
    """Returns the window class of the active window."""
    try:
        hwnd = _user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buf, 256)
        return buf.value or ""
    except Exception:
        return ""

def needs_double_click():
    """File Explorer, Desktop, and some list views need double-click to open."""
    cls = get_foreground_class().lower()
    # CabinetWClass = File Explorer window
    # Progman / WorkerW = Desktop
    # ExploreWClass = older explorer views
    return any(k in cls for k in ("cabinetwclass", "progman", "workerw", "explorewclass"))

def smart_click(x, y):
    """Automatically double-click in File Explorer / Desktop, single-click elsewhere."""
    if needs_double_click():
        print("  [Smart] File Explorer / Desktop detected -> double click")
        reliable_double_click(x, y)
    else:
        reliable_click(x, y)


# ---- Direct Virtual-Key presses via SendInput (works everywhere) ----------
VK_VOLUME_MUTE   = 0xAD
VK_VOLUME_DOWN   = 0xAE
VK_VOLUME_UP     = 0xAF
VK_MEDIA_PLAY    = 0xB3
VK_MEDIA_NEXT    = 0xB0
VK_MEDIA_PREV    = 0xB1

INPUT_KEYBOARD      = 1
KEYEVENTF_KEYUP     = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.c_ushort),
        ("wScan",       ctypes.c_ushort),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class INPUT_UNION_KB(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

class INPUT_KB(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [("type", ctypes.c_ulong), ("_u", INPUT_UNION_KB)]

def _send_key(vk, key_up=False):
    flags = KEYEVENTF_EXTENDEDKEY
    if key_up:
        flags |= KEYEVENTF_KEYUP
    inp = INPUT_KB(type=INPUT_KEYBOARD,
                   _u=INPUT_UNION_KB(ki=KEYBDINPUT(wVk=vk, dwFlags=flags)))
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT_KB))

def press_vk(vk, times=1, delay=0.08):
    """Press a virtual-key N times via SendInput (reliable for media keys)."""
    for _ in range(times):
        _send_key(vk, key_up=False)
        time.sleep(0.03)
        _send_key(vk, key_up=True)
        time.sleep(delay)


# ---- Mouse movement (relative) --------------------------------------------
def move_mouse(direction, amount=200):
    """Move cursor by N pixels in a direction."""
    try:
        from ctypes import wintypes
        amount = int(amount)
    except Exception:
        amount = 200

    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    x, y = pt.x, pt.y

    dx, dy = 0, 0
    d = direction.lower().strip()
    
    # Normalize compound directions
    d = d.replace("upper ", "up ").replace("lower ", "down ")
    
    # Parse direction - STRICT, no fallback to scroll
    if "up" in d and "right" in d:      dx, dy = amount, -amount
    elif "up" in d and "left" in d:     dx, dy = -amount, -amount
    elif "down" in d and "right" in d:  dx, dy = amount, amount
    elif "down" in d and "left" in d:   dx, dy = -amount, amount
    elif "right" in d:                  dx = amount
    elif "left" in d:                   dx = -amount
    elif "up" in d:                     dy = -amount
    elif "down" in d:                   dy = amount
    else:
        # NEVER default to scroll - just fail gracefully
        print(f"  [!] Unknown direction: '{direction}' - skipping")
        speak(f"Unknown direction")
        return

    screen_w, screen_h = pyautogui.size()
    new_x = max(0, min(screen_w - 1, x + dx))
    new_y = max(0, min(screen_h - 1, y + dy))

    # Size description for feedback
    if amount <= 60:        size_desc = "a little"
    elif amount <= 120:     size_desc = "half"
    elif amount <= 250:     size_desc = ""
    elif amount <= 400:     size_desc = "a lot"
    else:                   size_desc = "far"

    speak(f"Moving {direction} {size_desc}".strip())
    _user32.SetCursorPos(new_x, new_y)
    print(f"  [Move] {direction} by {amount}px: ({x},{y}) -> ({new_x},{new_y})")


def click_here():
    """Click at the current mouse cursor position."""
    try:
        from ctypes import wintypes
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        x, y = pt.x, pt.y
        speak("Clicking here")
        smart_click(x, y)
    except Exception as e:
        print(f"  [!] Click here error: {e}")
        speak("Click failed")


def double_click_here():
    """Double click at the current mouse cursor position."""
    try:
        from ctypes import wintypes
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        x, y = pt.x, pt.y
        speak("Double clicking here")
        reliable_double_click(x, y)
    except Exception as e:
        print(f"  [!] Double click error: {e}")


def right_click_here():
    """Right click at the current mouse cursor position."""
    try:
        from ctypes import wintypes
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        x, y = pt.x, pt.y
        speak("Right clicking")
        _user32.SetCursorPos(x, y)
        time.sleep(0.04)
        # Right click via SendInput
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010
        down = INPUT(type=INPUT_MOUSE, _u=INPUT_UNION(mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_RIGHTDOWN)))
        up = INPUT(type=INPUT_MOUSE, _u=INPUT_UNION(mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_RIGHTUP)))
        _user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        time.sleep(0.08)
        _user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
    except Exception as e:
        print(f"  [!] Right click error: {e}")


# ---- Reliable scroll via mouse_event (big & smooth) -----------------------
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

def hw_scroll(clicks):
    """Scroll via win32 mouse_event. Positive = up, negative = down."""
    try:
        _user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(clicks) * WHEEL_DELTA, 0)
    except Exception:
        pyautogui.scroll(int(clicks) * 100)


# ---- Configuration ----------------------------------------------------------

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:1.5b"
OLLAMA_VISION_MODEL = "moondream:1.8b"

WAKE_WORDS = ["rolex", "hey rolex", "rolex mini", "rollex", "rolax", 
              "rolix", "rollix", "rolexx"]

# EXIT: Must include "rolex" word to prevent accidental exits
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

# SLEEP: Must include "rolex" word 
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
    """Check if text contains the wake word."""
    if not text:
        return False
    t = text.lower().strip()
    
    for w in WAKE_WORDS:
        # Use word boundary matching
        if re.search(r"\b" + re.escape(w) + r"\b", t):
            return True
        # Also check if the ENTIRE text IS the wake word
        if t == w:
            return True
    return False


def strip_wake(text):
    """Remove wake word from text, returning just the command."""
    if not text:
        return ""
    t = text.lower().strip()
    
    # Sort wake words longest first (so "hey rolex" matches before "rolex")
    sorted_wake = sorted(WAKE_WORDS, key=len, reverse=True)
    
    # Try removing from start first
    for w in sorted_wake:
        if t.startswith(w + " ") or t == w:
            remaining = t[len(w):].strip()
            # Clean up punctuation and fillers
            remaining = remaining.lstrip(",.!?;:")
            remaining = re.sub(r"^(please|can you|could you|would you|now)\s+", "", remaining)
            return remaining.strip()
    
    # Try removing from end
    for w in sorted_wake:
        if t.endswith(" " + w):
            remaining = t[:-len(w)].strip()
            remaining = remaining.rstrip(",.!?;:")
            return remaining.strip()
    
    # Try removing from middle (less common but possible)
    for w in sorted_wake:
        pattern = r"\b" + re.escape(w) + r"\b"
        if re.search(pattern, t):
            new = re.sub(pattern, "", t, count=1).strip()
            new = re.sub(r"\s+", " ", new)  # collapse multiple spaces
            new = new.strip(",.!?;: ")
            return new
    
    return t  # No wake word found, return as-is


def is_exit(text):
    """
    Check if user wants to EXIT the program.
    MUST contain the word 'rolex' to prevent accidental exits.
    """
    if not text:
        return False
    t = text.lower().strip()
    
    # Safety check: must have "rolex" in the command
    if not has_wake(t):
        return False
    
    # Check if any exit phrase matches
    for phrase in EXIT_PHRASES:
        if phrase in t:
            return True
    
    # Also check: "rolex" + any exit keyword
    exit_keywords = ["exit", "quit", "shutdown", "shut down", "close yourself",
                     "terminate", "turn off", "power off", "kill yourself",
                     "goodbye", "die", "stop forever"]
    for kw in exit_keywords:
        if kw in t:
            return True
    
    return False


def is_sleep_command(text):
    """
    Check if user wants Rolex to SLEEP.
    MUST contain the word 'rolex' to prevent accidental sleep.
    """
    if not text:
        return False
    t = text.lower().strip()
    
    # Safety check: must have "rolex" in the command
    if not has_wake(t):
        return False
    
    # Check if any sleep phrase matches
    for phrase in SLEEP_PHRASES:
        if phrase in t:
            return True
    
    # Also check: "rolex" + any sleep keyword
    sleep_keywords = ["sleep", "goodnight", "good night", "standby", "stand by",
                      "stop listening", "pause listening", "be quiet", "quiet mode",
                      "silent mode", "rest now", "take a break", "pause"]
    for kw in sleep_keywords:
        # Pause alone could be play/pause media - must have "listening" or be "pause rolex"
        if kw == "pause" and "listening" not in t and "rolex" not in t:
            continue
        if kw in t:
            return True
    
    return False

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

# ---- Redis Cache for Vision & Screen (fast image recognition) --------------
try:
    import redis
    import hashlib

    _redis_client = redis.Redis(
        host="localhost", 
        port=6379, 
        db=0,
        decode_responses=False,
        socket_timeout=2,
        socket_connect_timeout=2,
    )
    # Test connection
    _redis_client.ping()
    _REDIS_AVAILABLE = True
    print("  [OK] Redis connected - vision caching enabled")
except Exception as _e:
    _redis_client = None
    _REDIS_AVAILABLE = False
    print(f"  [!] Redis not available ({_e}) - using memory cache")


def redis_cache_key(prefix, data):
    """Generate a cache key from prefix + data hash."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    h = hashlib.md5(data).hexdigest()[:16]
    return f"rolex:{prefix}:{h}"


def redis_get(key):
    """Get value from Redis, return None if not found."""
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
    """Store value in Redis with TTL (seconds)."""
    if not _REDIS_AVAILABLE:
        return
    try:
        _redis_client.setex(key, ttl, json.dumps(value).encode("utf-8"))
    except Exception as e:
        print(f"  [!] Redis set error: {e}")


def redis_cache_screenshot_hash():
    """Get a hash of the current screen (for cache invalidation)."""
    try:
        screenshot = pyautogui.screenshot()
        # Downsample heavily for fast hashing
        thumb = screenshot.resize((64, 36), Image.LANCZOS)
        buf = io.BytesIO()
        thumb.save(buf, format="JPEG", quality=20)
        return hashlib.md5(buf.getvalue()).hexdigest()[:12]
    except Exception:
        return str(int(time.time()))


# ---- Cache ------------------------------------------------------------------

_cmd_cache = {}
_ollama_ok_cache = {"val": None, "time": 0}
_vision_ok_cache = {"val": None, "time": 0}


def cached_ollama(prompt):
    # Redis first
    if _REDIS_AVAILABLE:
        rkey = redis_cache_key("ollama", prompt)
        cached = redis_get(rkey)
        if cached is not None:
            return cached.get("result")

    # Memory cache
    if prompt in _cmd_cache:
        return _cmd_cache[prompt]

    result = _ask_ollama_raw(prompt)
    if result:
        if len(_cmd_cache) > 200:
            _cmd_cache.clear()
        _cmd_cache[prompt] = result

        # Store in Redis
        if _REDIS_AVAILABLE:
            rkey = redis_cache_key("ollama", prompt)
            redis_set(rkey, {"result": result}, ttl=300)

    return result


# ---- Microphone -------------------------------------------------------------

def get_mic_settings():
    try:
        dev_idx = sd.default.device[0]
        if dev_idx < 0:
            return 1, 44100
        info = sd.query_devices(dev_idx)
        ch = min(int(info["max_input_channels"]), 2)
        sr = int(info["default_samplerate"])
        print(f"  [Mic] {info['name']} ({ch}ch @ {sr}Hz)")
        return max(ch, 1), sr
    except Exception as e:
        print(f"  [!] Mic error: {e}")
        return 1, 44100

MIC_CHANNELS, MIC_SAMPLE_RATE = get_mic_settings()
SILENCE_THRESHOLD = 400
SILENCE_DURATION = 1.0   # stop listening sooner after you stop talking
MAX_RECORD_SECONDS = 10

# ---- TTS --------------------------------------------------------------------

print("  [*] Initializing voice engine...")
engine = pyttsx3.init()
voices = engine.getProperty("voices")
for v in voices:
    if "zira" in v.name.lower() or "david" in v.name.lower():
        engine.setProperty("voice", v.id)
        break
engine.setProperty("rate", 180)
engine.setProperty("volume", 0.9)
tts_lock = threading.Lock()

# Short, fast speech — use async mode so actions don't wait
_speech_queue = []
_speech_thread = None

def _speech_worker():
    global _speech_queue
    while _speech_queue:
        txt = _speech_queue.pop(0)
        try:
            with tts_lock:
                engine.say(txt)
                engine.runAndWait()
        except Exception:
            pass

def speak(text, wait=False):
    """Non-blocking by default. Pass wait=True to block (rarely needed)."""
    global _speech_thread
    print(f"  [Astra] {text}")
    if wait:
        with tts_lock:
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception:
                pass
        return
    _speech_queue.append(text)
    if _speech_thread is None or not _speech_thread.is_alive():
        _speech_thread = threading.Thread(target=_speech_worker, daemon=True)
        _speech_thread.start()


# ---- Audio ------------------------------------------------------------------

def record_audio(max_seconds=MAX_RECORD_SECONDS):
    frames = []
    silence_count = 0
    has_speech = False
    block_dur = 0.1
    block_size = int(MIC_SAMPLE_RATE * block_dur)
    max_blocks = int(max_seconds / block_dur)
    silence_needed = int(SILENCE_DURATION / block_dur)

    try:
        stream = sd.InputStream(
            samplerate=MIC_SAMPLE_RATE, channels=MIC_CHANNELS,
            dtype="int16", blocksize=block_size,
        )
        stream.start()
        for _ in range(max_blocks):
            data, _ = stream.read(block_size)
            mono = data[:, 0] if data.ndim > 1 else data.flatten()
            frames.append(mono.copy())
            rms = np.sqrt(np.mean(mono.astype(np.float32) ** 2))
            if rms > SILENCE_THRESHOLD:
                has_speech = True
                silence_count = 0
            else:
                silence_count += 1
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


# ---- STT (uses Google - requires internet) ---------------------------------

try:
    import speech_recognition as _sr_module
except ImportError:
    _sr_module = None
    print("  [!] speech_recognition not installed")


def transcribe(audio):
    if _sr_module is None:
        return None
    try:
        wav = audio_to_wav_bytes(audio)
        rec = _sr_module.Recognizer()
        audio_data = _sr_module.AudioData(wav, 16000, 2)
        return rec.recognize_google(audio_data).lower().strip()
    except Exception as e:
        print(f"  [!] Transcription error: {e}")
        return None


def listen(prompt=None, timeout=MAX_RECORD_SECONDS):
    if prompt:
        speak(prompt)
    print("  [*] Listening...")
    audio = record_audio(max_seconds=timeout)
    if audio is None:
        return None
    print("  [*] Recognizing...")
    text = transcribe(audio)
    if text:
        print(f"  [Heard] \"{text}\"")
    return text


# ---- Continuous action control --------------------------------------------
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
    "rolex stop", "stop rolex"
]

def is_stop_command(text):
    """Check if text contains a stop command."""
    if not text:
        return False
    t = text.lower().strip()
    # Don't false-trigger on setup commands like "scroll down until stop"
    if any(k in t for k in ("until", "till", "keep scrolling", "keep moving", "forever", "continuously")):
        return False
    # Don't false-trigger on key holding like "hold shift" or "hold down ctrl"
    if re.search(r"\bhold\s+(?:down\s+)?(?:the\s+)?\w+\b", t):
        return False
    for phrase in STOP_PHRASES:
        if re.search(r"\b" + re.escape(phrase) + r"\b", t):
            return True
    return False

def stop_continuous_action():
    """Signal any running continuous action to stop."""
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
    """
    Spawn a listener that specifically watches for stop commands.
    Runs while a continuous action is active.
    """
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
    """Scroll continuously until user says stop."""
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
    time.sleep(0.3)  # let TTS finish before mic opens

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
    """Move mouse continuously until stopped."""
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
    d = d.replace("upper ", "up ").replace("lower ", "down ")
    
    dx, dy = 0, 0
    if "up" in d and "right" in d:      dx, dy = step, -step
    elif "up" in d and "left" in d:     dx, dy = -step, -step
    elif "down" in d and "right" in d:  dx, dy = step, step
    elif "down" in d and "left" in d:   dx, dy = -step, step
    elif "right" in d:                  dx = step
    elif "left" in d:                   dx = -step
    elif "up" in d:                     dy = -step
    elif "down" in d:                   dy = step
    else:
        speak(f"Unknown direction {direction}")
        return

    speak(f"Moving {direction}. Say stop when done.")
    time.sleep(0.3)

    _continuous_action["running"] = True
    _continuous_action["stop_flag"].clear()

    def _move_loop():
        try:
            from ctypes import wintypes
            screen_w, screen_h = pyautogui.size()

            while not _continuous_action["stop_flag"].is_set():
                pt = wintypes.POINT()
                _user32.GetCursorPos(ctypes.byref(pt))
                new_x = max(0, min(screen_w - 1, pt.x + dx))
                new_y = max(0, min(screen_h - 1, pt.y + dy))

                if new_x == pt.x and new_y == pt.y:
                    print("  [Move] Hit screen edge, stopping")
                    break

                _user32.SetCursorPos(new_x, new_y)

                if _continuous_action["stop_flag"].wait(cfg["delay"]):
                    break
        finally:
            _continuous_action["running"] = False
            print("  [Move] Stopped")

    threading.Thread(target=_move_loop, daemon=True).start()
    listen_for_stop_async()


# ---- Ollama -----------------------------------------------------------------

def ollama_ok():
    # Cache for 30s to avoid HTTP spam
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
    try:
        import requests
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt,
                  "stream": False, "options": {"temperature": 0.1, "num_predict": 30}},
            timeout=20,
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


# ---- Ollama Command Translator (fallback for unknown commands) ------------

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

# Build the known commands reference ONCE (used in every prompt)
KNOWN_COMMANDS_REFERENCE = """
KNOWN COMMAND FORMATS (translate user input to ONE of these):

APPS:
- open chrome
- open vlc
- open notepad
- open calculator
- open file explorer
- open settings
- open youtube
- open 2nd chrome account

CLICK (on text/words visible on screen):
- click [word]
- click on [word]
- click 2nd [word]
- click third button

CLICK AT CURSOR POSITION:
- click here
- double click
- right click

MOUSE MOVEMENT:
- go right
- go left  
- go up
- go down
- go up right
- move right half
- go down a little
- go up a lot
- move left 300

SCROLL (MUST include up or down):
- scroll up
- scroll down
- scroll up a lot
- scroll down a little
- scroll to top
- scroll to bottom

NEVER return just "scroll" without a direction.

MEDIA:
- play
- pause
- volume up
- volume down
- mute
- forward
- backward
- fullscreen

TABS (browser):
- go to tab 4
- next tab
- previous tab
- close tab
- close 3rd tab
- new tab
- go to gmail tab

WINDOWS (current window):
- minimize
- minimize this
- maximize
- maximize this
- restore this
- close this

WINDOWS (by app name):
- minimize chrome
- minimize 2nd chrome
- minimize all chrome
- maximize vlc
- maximize all vlc
- restore chrome
- close chrome
- close 2nd chrome
- close all chrome windows
- switch to chrome

WINDOWS (bulk):
- minimize all
- maximize all
- restore all
- close all minimized
- show desktop
- list windows

TYPE/SEARCH:
- type [text]
- search for [query]
- google [query]
- search youtube for [query]

KEYBOARD:
- press enter
- press escape
- press tab
- go back
- screenshot

GRID:
- click 2nd video in 3rd row
- click first image

SELECT:
- select [word]

CONTINUOUS ACTIONS (run until stop):
- scroll down until stop
- scroll up until stop
- keep scrolling down
- scroll down continuously
- move right until stop
- keep moving down
- move left continuously

STOP COMMANDS:
- stop
- stop it
- that's enough
- enough

SLEEP/EXIT:
- go to sleep
- exit

MULTI-STEP (complex commands broken into steps):
- open jersey in movies in file explorer
- search for cats on youtube in chrome
- open resume in documents folder
- minimize chrome and open vlc
- go to downloads then open the first file
- open chrome and search for weather
"""

def ollama_translate_command(user_text):
    """
    Send unrecognized command to Ollama with context of known commands.
    Returns a translated command that matches our parser's known formats.
    """
    cache_key = user_text.lower().strip()
    if cache_key in _translation_cache:
        cached = _translation_cache[cache_key]
        print(f"  [Learned] \"{user_text}\" -> \"{cached}\"")
        return cached

    if not ollama_ok():
        return None

    prompt = f"""You are a command translator for a voice assistant called Rolex. 
The user said something that doesn't exactly match the known command formats.
Translate their request into the CLOSEST matching known command.

{KNOWN_COMMANDS_REFERENCE}

CRITICAL RULES:
1. Reply with ONLY the translated command - no explanation.
2. NEVER return just "scroll" alone - always include "up" or "down".
3. If the request is NOT a clear command, reply: unknown
4. If user wants mouse movement, use "go [direction]" NOT scroll.
5. If user asks for info/questions (what time, weather, etc), reply: unknown
6. If the request has nothing to do with any command above, reply: unknown
7. Do NOT guess - if unclear, reply: unknown

Examples:
User: "take me to the right side"       -> go right
User: "shift mouse up a bit"            -> go up a little  
User: "I want to see more content"      -> scroll down
User: "scroll"                          -> unknown
User: "move the page"                   -> unknown
User: "do something"                    -> unknown
User: "what time is it"                 -> unknown
User: "how are you"                     -> unknown
User: "fire up spotify"                 -> open spotify
User: "quiet down please"               -> volume down
User: "make this window go away"        -> close this
User: "jump to the second tab"          -> go to tab 2
User: "hit the submit button"           -> click submit
User: "mash it"                         -> click here

Now translate this:
User: "{user_text}"
Translated:"""

    try:
        import requests
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 25}
            },
            timeout=15,
        )
        if resp.status_code == 200:
            answer = resp.json().get("response", "").strip()
            # Clean up the response
            answer = answer.split("\n")[0].strip()
            answer = answer.strip("\"'.,!?")
            answer = answer.lower()
            # Strip common prefixes the model might add
            for prefix in ("translated:", "command:", "answer:", "->"):
                if answer.startswith(prefix):
                    answer = answer[len(prefix):].strip()

            if answer and answer != "unknown" and len(answer) < 100:
                print(f"  [Ollama Translator] \"{user_text}\" -> \"{answer}\"")
                _translation_cache[cache_key] = answer
                save_translations()
                return answer
            else:
                print(f"  [Ollama Translator] Could not translate")
    except Exception as e:
        print(f"  [!] Translation error: {e}")

    return None


# ---- Vision -----------------------------------------------------------------

_vision_cache = {}


def vision_model_ok():
    now = time.time()
    if _vision_ok_cache["val"] is not None and now - _vision_ok_cache["time"] < 30:
        return _vision_ok_cache["val"]
    try:
        import requests
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        models = r.json().get("models", [])
        val = any("moondream" in m.get("name", "") or "llava" in m.get("name", "")
                   for m in models)
    except Exception:
        val = False
    _vision_ok_cache["val"] = val
    _vision_ok_cache["time"] = now
    return val


def find_image_on_screen(description):
    """Find an image/element on screen using vision model with Redis cache."""
    cache_key_desc = description.lower().strip()

    # ---- Try Redis cache first (much faster) ----
    if _REDIS_AVAILABLE:
        screen_hash = redis_cache_screenshot_hash()
        rkey = redis_cache_key("vision", f"{cache_key_desc}:{screen_hash}")
        cached = redis_get(rkey)
        if cached is not None:
            if cached.get("found"):
                coords = tuple(cached["coords"])
                print(f"  [Redis Cache] Found \"{description}\" at {coords}")
                return coords
            else:
                print(f"  [Redis Cache] \"{description}\" was not found (cached)")
                return None

    # ---- Memory cache fallback ----
    if cache_key_desc in _vision_cache:
        mem = _vision_cache[cache_key_desc]
        if time.time() - mem["time"] < 10:
            print(f"  [Mem Cache] Using cached result for \"{description}\"")
            return mem["coords"]

    print(f"  [Vision] Analyzing screen for: \"{description}\"")

    try:
        screenshot = pyautogui.screenshot()
    except Exception as e:
        print(f"  [!] Screenshot failed: {e}")
        return None
    screen_w, screen_h = screenshot.size

    # Resize for speed
    max_dim = 768  # smaller = faster (was 1024)
    if screen_w > max_dim or screen_h > max_dim:
        ratio = min(max_dim / screen_w, max_dim / screen_h)
        new_w = int(screen_w * ratio)
        new_h = int(screen_h * ratio)
        screenshot_small = screenshot.resize((new_w, new_h), Image.LANCZOS)
        scale_x = screen_w / new_w
        scale_y = screen_h / new_h
    else:
        screenshot_small = screenshot
        scale_x = scale_y = 1.0
        new_w, new_h = screen_w, screen_h

    buf = io.BytesIO()
    screenshot_small.save(buf, format="JPEG", quality=60)  # lower quality = faster
    img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    prompt = (
        f"Look at this screenshot ({new_w}x{new_h} pixels). "
        f"Find the \"{description}\" in the image. "
        f"Reply with ONLY the approximate pixel coordinates of its CENTER "
        f"in this format: x,y\n"
        f"If you cannot find it, reply: not found"
    )

    # ---- Send to Ollama (via Redis queue if available) ----
    result_coords = None

    try:
        import requests

        # Use async request with shorter timeout
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_VISION_MODEL,
                "prompt": prompt,
                "images": [img_b64],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 20}
            },
            timeout=25,
        )
        if resp.status_code == 200:
            answer = resp.json().get("response", "").strip()
            print(f"  [Vision] Raw answer: \"{answer}\"")

            if "not found" in answer.lower():
                result_coords = None
            else:
                coord_match = re.search(r"(\d+)\s*[,;\s]\s*(\d+)", answer)
                if coord_match:
                    x = int(coord_match.group(1))
                    y = int(coord_match.group(2))
                    real_x = int(x * scale_x)
                    real_y = int(y * scale_y)
                    if 0 <= real_x <= screen_w and 0 <= real_y <= screen_h:
                        result_coords = (real_x, real_y)
                        print(f"  [Vision] Found at ({real_x}, {real_y})")
    except Exception as e:
        print(f"  [!] Vision error: {e}")

    # ---- Store in both caches ----
    _vision_cache[cache_key_desc] = {"coords": result_coords, "time": time.time()}

    if _REDIS_AVAILABLE:
        screen_hash = redis_cache_screenshot_hash()
        rkey = redis_cache_key("vision", f"{cache_key_desc}:{screen_hash}")
        if result_coords:
            redis_set(rkey, {"found": True, "coords": list(result_coords)}, ttl=30)
        else:
            redis_set(rkey, {"found": False, "coords": None}, ttl=15)

    return result_coords


def is_image_query(text):
    image_words = [
        "image", "picture", "photo", "icon", "logo", "thumbnail",
        "avatar", "emoji", "symbol", "graphic", "banner",
        "the one with", "that looks like", "the picture of",
        "that has", "showing",
    ]
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


# ---- OCR --------------------------------------------------------------------

try:
    import winocr as _winocr_module
    _WINOCR_AVAILABLE = True
except ImportError:
    _winocr_module = None
    _WINOCR_AVAILABLE = False
    print("  [!] winocr not installed - screen text reading disabled")

_ocr_cache = {"words": None, "time": 0}
OCR_CACHE_SECONDS = 1.5

def ocr_screen(force=False):
    """OCR with Redis + memory cache."""
    now = time.time()

    # Memory cache (fastest)
    if (not force and _ocr_cache["words"] is not None
            and now - _ocr_cache["time"] < OCR_CACHE_SECONDS):
        return _ocr_cache["words"]

    # Redis cache
    if not force and _REDIS_AVAILABLE:
        screen_hash = redis_cache_screenshot_hash()
        rkey = redis_cache_key("ocr", screen_hash)
        cached = redis_get(rkey)
        if cached is not None:
            print(f"  [Redis OCR] Using cached OCR ({len(cached)} words)")
            _ocr_cache["words"] = cached
            _ocr_cache["time"] = now
            return cached

    words = []
    if not _WINOCR_AVAILABLE:
        return words
    try:
        screenshot = pyautogui.screenshot()
        result = asyncio.run(_winocr_module.recognize_pil(screenshot, lang="en"))
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

    # Store in both caches
    _ocr_cache["words"] = words
    _ocr_cache["time"] = now

    if _REDIS_AVAILABLE and words:
        screen_hash = redis_cache_screenshot_hash()
        rkey = redis_cache_key("ocr", screen_hash)
        redis_set(rkey, words, ttl=10)

    return words


def find_on_screen(target, occurrence=1, prefer_region=None):
    print(f"  [*] Scanning screen for: \"{target}\" (occurrence #{occurrence})")
    words = ocr_screen()
    if not words:
        print("  [!] No text on screen.")
        return None

    target_low = target.lower().strip()
    target_parts = target_low.split()
    screen_h = pyautogui.size()[1]

    matches = []

    for w in words:
        score = 0
        if w["text"] == target_low:
            score = 1.0
        elif target_low in w["text"] or w["text"] in target_low:
            score = SequenceMatcher(None, target_low, w["text"]).ratio()
        else:
            r = SequenceMatcher(None, target_low, w["text"]).ratio()
            if r > 0.6:
                score = r
        if score > 0.5:
            matches.append({"word": w, "score": score})

    # Multi-word: sort words by position first to get reading order
    if len(target_parts) > 1:
        sorted_words = sorted(words, key=lambda w: (w["y"] // 20, w["x"]))
        texts = [w["text"] for w in sorted_words]
        for i in range(len(texts) - len(target_parts) + 1):
            chunk = " ".join(texts[i:i + len(target_parts)])
            r = SequenceMatcher(None, target_low, chunk).ratio()
            if r > 0.7:
                mid = i + len(target_parts) // 2
                matches.append({"word": sorted_words[mid], "score": r})

    if not matches:
        print(f"  [X] \"{target}\" not found.")
        return None

    matches.sort(key=lambda m: m["score"], reverse=True)

    if prefer_region and len(matches) > 1:
        for m in matches:
            y = m["word"]["y"]
            if prefer_region == "top" and y < screen_h * 0.33:
                m["score"] += 0.1
            elif prefer_region == "bottom" and y > screen_h * 0.66:
                m["score"] += 0.1
            elif prefer_region == "center" and screen_h * 0.2 < y < screen_h * 0.8:
                m["score"] += 0.05
        matches.sort(key=lambda m: m["score"], reverse=True)

    if occurrence > len(matches):
        occurrence = len(matches)
    pick = matches[occurrence - 1]

    w = pick["word"]
    print(f"  [OK] Match #{occurrence}: \"{w['text']}\" (score={pick['score']:.2f}) at ({w['x']},{w['y']})")
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


# ---- Command Parser ---------------------------------------------------------

def parse_number_words(text):
    # Check explicit digits first (e.g. "15", "10", "30")
    m = re.search(r"\b(\d+)\b", text)
    if m:
        return int(m.group(1))

    # Strip time units so "15 seconds" doesn't match ordinal "second"
    clean = re.sub(r"\bseconds?\b|\bsecs?\b", "", text)

    # Word numbers
    word_map = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
        "sixty": 60,
    }
    for word, num in word_map.items():
        if re.search(r"\b" + re.escape(word) + r"\b", clean):
            return num

    for word, num in ORDINALS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", clean):
            return num
    return None


def normalize_command(text):
    """Expand slang/synonyms into canonical verbs so the parser catches them."""
    t = " " + text.lower().strip() + " "

    # Click here synonyms (must run before click_syn so "click at" doesn't swallow "click at cursor")
    t = t.replace(" click right here ", " click here ")
    t = t.replace(" click at mouse ", " click here ")
    t = t.replace(" click where mouse is ", " click here ")
    t = t.replace(" click at cursor ", " click here ")
    t = t.replace(" click at the cursor ", " click here ")
    t = t.replace(" click where cursor is ", " click here ")
    t = t.replace(" tap right here ", " click here ")
    t = t.replace(" tap at cursor ", " click here ")
    t = t.replace(" tap at mouse ", " click here ")
    t = t.replace(" just click ", " click here ")
    t = t.replace(" click it ", " click here ")
    t = t.replace(" click now ", " click here ")

    # Click synonyms
    click_syn = ["click on", "click at", "tap on", "tap at", "press on",
                 "hit on", "touch", "activate", "trigger", "pick",
                 "choose on", "select"]
    for syn in click_syn:
        t = t.replace(f" {syn} ", " click ")

    # Open synonyms
    open_syn = ["bring up", "pull up", "boot up", "fire up", "load up",
                "load", "launch", "start up", "boot", "fire up"]
    for syn in open_syn:
        t = t.replace(f" {syn} ", " open ")

    # Close synonyms
    close_syn = ["shut", "kill", "terminate", "end", "dismiss", "get rid of",
                 "remove", "exit out of", "exit"]
    for syn in close_syn:
        t = t.replace(f" {syn} this ", " close this ")
        t = t.replace(f" {syn} window ", " close window ")
        t = t.replace(f" {syn} tab ", " close tab ")

    # Volume synonyms
    t = t.replace(" turn it up ", " volume up ")
    t = t.replace(" turn it down ", " volume down ")
    t = t.replace(" make it louder ", " volume up ")
    t = t.replace(" make it quieter ", " volume down ")
    t = t.replace(" sound up ", " volume up ")
    t = t.replace(" sound down ", " volume down ")
    t = t.replace(" raise volume ", " volume up ")
    t = t.replace(" lower volume ", " volume down ")
    t = t.replace(" crank it ", " volume up ")
    t = t.replace(" silence ", " mute ")
    t = t.replace(" shut up ", " mute ")

    # Scroll synonyms (STRICT - only exact phrases)
    # Note: "page down" / "page up" are preserved so parse_command sets amount 15

    # Media synonyms
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

    # Mouse movement synonyms
    t = t.replace(" cursor goes ", " move ")
    t = t.replace(" pointer ", " cursor ")
    t = t.replace(" bring cursor ", " move cursor ")
    t = t.replace(" shift cursor ", " move cursor ")
    t = t.replace(" drag cursor ", " move cursor ")

    # "go to right" -> "go right" (strip "to" after move verbs for directions)
    t = re.sub(r"\b(go|move|cursor|mouse|shift|slide)\s+to\s+(the\s+)?(right|left|up|down|upper|lower)\b",
               r"\1 \3", t)

    # "go to the right" patterns
    t = t.replace(" go to right ", " go right ")
    t = t.replace(" go to left ", " go left ")
    t = t.replace(" go to up ", " go up ")
    t = t.replace(" go to down ", " go down ")
    t = t.replace(" go to the right ", " go right ")
    t = t.replace(" go to the left ", " go left ")
    t = t.replace(" go to the top ", " go up ")
    t = t.replace(" go to the bottom ", " go down ")
    t = t.replace(" move to right ", " move right ")
    t = t.replace(" move to left ", " move left ")
    t = t.replace(" move to top ", " move up ")
    t = t.replace(" move to bottom ", " move down ")

    # Half synonyms
    t = t.replace(" halfway ", " half ")
    t = t.replace(" half way ", " half ")
    t = t.replace(" by half ", " half ")
    t = t.replace(" fifty percent ", " half ")
    t = t.replace(" 50 percent ", " half ")
    t = t.replace(" medium amount ", " half ")
    t = t.replace(" medium ", " half ")

    # Click here synonyms
    t = t.replace(" click right here ", " click here ")
    t = t.replace(" click at mouse ", " click here ")
    t = t.replace(" click where mouse is ", " click here ")
    t = t.replace(" click at the cursor ", " click here ")
    t = t.replace(" click where cursor is ", " click here ")
    t = t.replace(" tap right here ", " click here ")
    t = t.replace(" just click ", " click here ")
    t = t.replace(" click it ", " click here ")
    t = t.replace(" click now ", " click here ")

    # Window state synonyms
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
    t = t.replace(" enlarge ", " fullscreen ")

    # "window" standalone context
    t = t.replace(" minimize window ", " minimize this ")
    t = t.replace(" maximize window ", " maximize this ")

    # Continuous action synonyms
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

    # Stop synonyms  
    t = t.replace(" cut it out ", " stop ")
    t = t.replace(" quit that ", " stop ")
    t = t.replace(" knock it off ", " stop ")
    t = t.replace(" that's good ", " stop ")
    t = t.replace(" thats good ", " stop ")
    t = t.replace(" good enough ", " stop ")

    # Search
    t = t.replace(" look for ", " search for ")
    t = t.replace(" find info on ", " search for ")
    t = t.replace(" find me ", " search for ")
    t = t.replace(" google me ", " google ")

    # Navigation
    t = t.replace(" previous page ", " go back ")
    t = t.replace(" last page ", " go back ")
    t = t.replace(" undo navigation ", " go back ")

    # Screenshot
    t = t.replace(" take picture ", " screenshot ")
    t = t.replace(" capture screen ", " screenshot ")
    t = t.replace(" snap screen ", " screenshot ")
    t = t.replace(" print screen ", " screenshot ")

    # Tab operations
    t = t.replace(" fresh tab ", " new tab ")
    t = t.replace(" another tab ", " new tab ")
    t = t.replace(" open a tab ", " new tab ")
    t = t.replace(" new window tab ", " new tab ")

    # Keyboard & Key press synonyms
    t = t.replace(" hit enter ", " press enter ")
    t = t.replace(" hit return ", " press enter ")
    t = t.replace(" submit ", " press enter ")
    t = t.replace(" confirm ", " press enter ")
    t = t.replace(" dismiss ", " press escape ")
    t = t.replace(" close dialog ", " press escape ")

    t = t.replace(" press the key ", " press ")
    t = t.replace(" press the ", " press ")
    t = t.replace(" push the key ", " press ")
    t = t.replace(" push ", " press ")
    t = t.replace(" smash ", " press ")
    t = t.replace(" strike ", " press ")
    t = t.replace(" keyboard shortcut ", " press ")
    t = t.replace(" shortcut ", " press ")
    t = t.replace(" hotkey ", " press ")
    t = t.replace(" key combo ", " press ")
    t = t.replace(" key combination ", " press ")

    # Common combos spoken as words
    t = t.replace(" copy ", " press ctrl c ")
    t = t.replace(" paste ", " press ctrl v ")
    t = t.replace(" cut ", " press ctrl x ")
    t = t.replace(" undo ", " press ctrl z ")
    t = t.replace(" redo ", " press ctrl y ")
    t = t.replace(" save as ", " press ctrl shift s ")
    t = t.replace(" save ", " press ctrl s ")
    t = t.replace(" select all ", " press ctrl a ")
    t = t.replace(" find ", " press ctrl f ")
    t = t.replace(" refresh ", " press f5 ")
    t = t.replace(" reload ", " press f5 ")

    # Repeat word variants
    t = t.replace(" couple times ", " 2 times ")
    t = t.replace(" couple of times ", " 2 times ")
    t = t.replace(" few times ", " 3 times ")
    t = t.replace(" several times ", " 5 times ")
    t = t.replace(" many times ", " 10 times ")

    # Window management synonyms
    t = t.replace(" show the desktop ", " show desktop ")
    t = t.replace(" hide everything ", " minimize all ")
    t = t.replace(" clear screen ", " minimize all ")
    t = t.replace(" bring back ", " restore ")
    t = t.replace(" what windows are open ", " list windows ")

    # Sleep synonyms (go to sleep mode)
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
    
    # Exit synonyms (close program entirely)
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

    # Multi-step connectors (normalize)
    t = t.replace(" and after that ", " then ")
    t = t.replace(" after that ", " then ")
    t = t.replace(" afterwards ", " then ")
    t = t.replace(" following that ", " then ")
    t = t.replace(" once done ", " then ")
    t = t.replace(" when done ", " then ")
    t = t.replace(" once that's done ", " then ")
    t = t.replace(" after this ", " then ")
    t = t.replace(" subsequently ", " then ")
    
    # Folder navigation words
    t = t.replace(" inside ", " in ")
    t = t.replace(" within ", " in ")
    t = t.replace(" under ", " in ")
    t = t.replace(" into ", " in ")
    t = t.replace(" from ", " in ")
    t = t.replace(" of ", " in ")
    
    # Path separator
    t = t.replace(" slash ", " in ")
    t = t.replace(" backslash ", " in ")
    t = t.replace(" -> ", " in ")
    t = t.replace(" > ", " in ")

    # Handle Rolex-prefixed commands properly
    # These should NOT be mis-parsed as separate verbs
    clean_t = t.strip()
    clean_t = re.sub(r"^rolex\s+", "", clean_t)  # Remove leading "rolex "
    clean_t = re.sub(r"\s+rolex$", "", clean_t)  # Remove trailing " rolex"

    return clean_t.strip()


# ---- Tab switching by number (Ctrl+1 through Ctrl+9) ----------------------
def switch_tab_number(num):
    """Switch to Nth tab using Ctrl+N shortcut (works in Chrome/Edge/Firefox)."""
    try:
        n = int(num)
    except Exception:
        n = 1
    if n < 1:
        n = 1
    if n > 9:
        speak("Maximum tab number is 9")
        n = 9
    speak(f"Switching to tab {n}")
    pyautogui.hotkey("ctrl", str(n))

def next_tab():
    speak("Next tab")
    pyautogui.hotkey("ctrl", "tab")

def prev_tab():
    speak("Previous tab")
    pyautogui.hotkey("ctrl", "shift", "tab")

def close_nth_tab(num):
    """Switch to Nth tab then close it."""
    try:
        n = int(num)
    except Exception:
        n = 1
    speak(f"Closing tab {n}")
    pyautogui.hotkey("ctrl", str(n))
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "w")


# ---- Context detection: what app is currently focused? --------------------
def get_foreground_window_info():
    """Returns (class_name, window_title, process_name) of active window."""
    try:
        hwnd = _user32.GetForegroundWindow()
        # Class name
        cls_buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls_buf, 256)
        cls = cls_buf.value or ""
        # Title
        length = _user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, title_buf, length + 1)
        title = title_buf.value or ""
        # Process
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

def get_context():
    """Return one of: 'browser', 'explorer', 'desktop', 'editor', 'other'."""
    cls, title, proc = get_foreground_window_info()
    
    # Browsers
    browser_procs = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", 
                     "opera.exe", "vivaldi.exe")
    if any(bp in proc for bp in browser_procs):
        return "browser"
    if "chrome_widgetwin" in cls or "mozillawindowclass" in cls:
        return "browser"
    
    # File Explorer
    if "cabinetwclass" in cls or "explorewclass" in cls:
        return "explorer"
    
    # Desktop
    if "progman" in cls or "workerw" in cls:
        return "desktop"
    
    # Text editors
    if proc in ("notepad.exe", "notepad++.exe", "code.exe", "sublime_text.exe"):
        return "editor"
    
    return "other"

def is_url(text):
    """Check if text looks like a URL or domain."""
    t = text.lower().strip()
    if t.startswith(("http://", "https://", "www.")):
        return True
    # Looks like a domain: word.com, word.org, etc.
    if re.match(r"^[\w-]+\.[a-z]{2,}(/.*)?$", t):
        return True
    return False


# ---- Smart context-aware OPEN and SEARCH ----------------------------------

def smart_open(target):
    """
    Context-aware 'open' command:
    1. Known app? -> Launch it
    2. URL/domain? -> Open in browser
    3. Browser focused? -> Open in new tab
    4. File Explorer focused? -> Search for file
    5. Otherwise? -> Windows search
    """
    target = target.strip().rstrip(".").lower()
    if not target:
        speak("Open what?")
        return
    
    # 1. Known app
    if target in APP_MAP:
        open_app(target)
        return
    for key in APP_MAP:
        if key == target or (len(target) > 3 and target in key) or (len(key) > 3 and key in target):
            open_app(key)
            return
    
    # 2. URL / domain
    if is_url(target):
        speak(f"Opening {target}")
        import webbrowser
        url = target if target.startswith(("http://", "https://")) else f"https://{target}"
        webbrowser.open(url)
        return
    
    # 3. Context-aware
    ctx = get_context()
    print(f"  [Context] Active: {ctx}")
    
    if ctx == "browser":
        # Open in new tab with Google search
        speak(f"Opening {target} in browser")
        pyautogui.hotkey("ctrl", "t")
        time.sleep(0.4)
        pyautogui.typewrite(target, interval=0.02)
        time.sleep(0.2)
        pyautogui.press("enter")
        return
    
    if ctx == "explorer":
        # Focus the search bar in File Explorer and search
        speak(f"Searching for {target} in Explorer")
        pyautogui.hotkey("ctrl", "e")  # Focus search box
        time.sleep(0.4)
        pyautogui.typewrite(target, interval=0.02)
        time.sleep(0.2)
        pyautogui.press("enter")
        return
    
    if ctx == "editor":
        # Open file dialog
        speak(f"Opening file {target}")
        pyautogui.hotkey("ctrl", "o")
        time.sleep(0.6)
        pyautogui.typewrite(target, interval=0.02)
        return
    
    # 4. Fallback: Windows start menu search
    speak(f"Searching Windows for {target}")
    pyautogui.press("win")
    time.sleep(1.0)
    pyautogui.typewrite(target, interval=0.03)
    time.sleep(1.5)
    pyautogui.press("enter")


def smart_search(query):
    """
    Context-aware 'search' command:
    - Browser focused? -> Search in current tab (address bar) or new tab
    - File Explorer? -> Search files
    - Otherwise -> Google search in default browser
    """
    query = query.strip().rstrip(".")
    if not query:
        speak("Search for what?")
        return
    
    ctx = get_context()
    print(f"  [Context] Active: {ctx}")
    
    if ctx == "browser":
        # Use address bar to search in current tab
        speak(f"Searching for {query}")
        pyautogui.hotkey("ctrl", "l")  # Focus address bar
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
    
    # Default: open Google in browser
    speak(f"Googling {query}")
    import webbrowser
    webbrowser.open(f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}")


def smart_search_youtube(query):
    """Search specifically on YouTube."""
    query = query.strip().rstrip(".")
    speak(f"Searching YouTube for {query}")
    import webbrowser
    webbrowser.open(f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}")


# ---- Multi-Step Command Planner (breaks complex commands into steps) ------

def is_multi_step_command(text):
    """Detect if a command needs multiple steps to execute."""
    t = text.lower().strip()
    
    # Multiple "then" / "and then" / "after that"
    if re.search(r"\b(then|after that|next|and then|afterwards)\b", t):
        return True
    
    # Path-like: "in X in Y in Z" / "inside X folder"
    if t.count(" in ") >= 2:
        return True
    
    # Navigation: "go to X then open Y"
    if re.search(r"(go to|navigate to|open).+(then|and|after).+(open|click|find|select)", t):
        return True
    
    # File explorer paths: "open X in Y folder"
    if re.search(r"open\s+.+\s+in\s+.+\s*(folder|directory|drive|disk)", t):
        return True
    
    # Complex open: "open X in Y in Z"
    if re.search(r"open\s+.+\s+in\s+.+\s+in\s+", t):
        return True
    
    # Chrome complex: "open youtube and search for X"
    if re.search(r"open\s+\w+\s+and\s+(search|type|go to|find|play)", t):
        return True
    
    # "find X in Y" with app context
    if re.search(r"(find|search for|look for)\s+.+\s+in\s+(file explorer|chrome|browser|explorer|downloads|documents|desktop|movies|music|pictures|videos)", t):
        return True
    
    # Multiple actions: "minimize chrome and open vlc"
    if re.search(r"(open|close|minimize|maximize|click)\s+.+\s+and\s+(open|close|minimize|maximize|click)", t):
        return True
    
    # Step-like: first...then / after opening...do
    if re.search(r"(first|after opening|after going|once you|when you)", t):
        return True
    
    return False


def plan_steps_with_ollama(user_text):
    """
    Send complex command to Ollama to break into sequential steps.
    Returns list of simple commands that our parser understands.
    """
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

IMPORTANT RULES:
1. Each step must be ONE simple command from the known commands list above.
2. Add "wait 1" between steps that need time to load (opening apps, clicking).
3. Add "wait 2" for apps that take longer to load.
4. If navigating folders in File Explorer:
   - First step: open file explorer (if not already open)
   - Then: click on each folder name one by one
   - Use "click [foldername]" for each folder
   - If folder needs double-click, just say "click [foldername]"
5. If navigating in Chrome/browser:
   - First step: open chrome (if not already open)  
   - Then: click address bar or new tab as needed
   - Then: type URL or search query
   - Then: press enter
6. Each step on its own numbered line.
7. Use "wait N" for pauses (N = seconds).
8. If a step might fail or be unclear, add "confirm" before it.
9. Only output the numbered steps, nothing else.
10. Use ONLY commands from the known commands list.

FILE EXPLORER FOLDER NAVIGATION:
- To go into a folder: click [foldername]
- The system auto-detects File Explorer and double-clicks
- For path like "Movies > Jersey": click movies, wait 1, click jersey

CHROME NAVIGATION:  
- To search: press ctrl l, wait 1, type [query], press enter
- To open URL: press ctrl l, wait 1, type [url], press enter
- New tab: press ctrl t, wait 1, type [query], press enter

EXAMPLES:

User: "open jersey in movies in file explorer"
Steps:
1. open file explorer
2. wait 2
3. click movies
4. wait 1
5. click jersey

User: "search for cats on youtube in chrome"
Steps:
1. open chrome
2. wait 2
3. press ctrl t
4. wait 1
5. type youtube.com
6. press enter
7. wait 2
8. click search
9. wait 1
10. type cats
11. press enter

User: "open my resume in documents folder"
Steps:
1. open file explorer
2. wait 2
3. click documents
4. wait 1
5. click resume

User: "minimize chrome and open vlc and play"
Steps:
1. minimize chrome
2. wait 1
3. open vlc
4. wait 2
5. play

User: "go to downloads folder and delete the first file"
Steps:
1. open file explorer
2. wait 2
3. click downloads
4. wait 1
5. click first item
6. press delete

Now plan this:
User: "{user_text}"
Steps:"""
    
    try:
        import requests
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 200}
            },
            timeout=30,
        )
        if resp.status_code == 200:
            answer = resp.json().get("response", "").strip()
            print(f"  [Planner] Raw plan:\n{answer}")
            return parse_plan(answer)
    except Exception as e:
        print(f"  [!] Planner error: {e}")
    
    return None


def parse_plan(plan_text):
    """Parse numbered plan into list of step dicts."""
    steps = []
    lines = plan_text.strip().split("\n")
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Remove numbering: "1. ", "1) ", "- "
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        line = re.sub(r"^[-•]\s*", "", line)
        line = line.strip()
        
        if not line:
            continue
        
        # Skip meta text
        skip_words = ["steps:", "plan:", "here", "following", "note:", 
                      "explanation", "first,", "the steps"]
        if any(line.lower().startswith(sw) for sw in skip_words):
            continue
        
        # Wait command
        wait_match = re.match(r"wait\s+(\d+)", line.lower())
        if wait_match:
            steps.append({
                "type": "wait",
                "seconds": int(wait_match.group(1)),
                "raw": line
            })
            continue
        
        # Confirm step (pause and ask user)
        if line.lower().startswith("confirm"):
            remaining = re.sub(r"^confirm\s*", "", line, flags=re.IGNORECASE).strip()
            steps.append({
                "type": "confirm",
                "message": remaining or "Should I continue?",
                "raw": line
            })
            continue
        
        # Regular command step
        steps.append({
            "type": "command",
            "command": line.lower().strip(),
            "raw": line
        })
    
    return steps if steps else None


def execute_plan(steps, user_text=""):
    """
    Execute a multi-step plan.
    Pauses on errors or 'confirm' steps.
    User can say "skip", "continue", "stop", "cancel".
    """
    if not steps:
        speak("No steps to execute")
        return
    
    total = len([s for s in steps if s["type"] == "command"])
    cmd_num = 0
    
    speak(f"I'll do this in {total} steps")
    time.sleep(0.3)
    
    for i, step in enumerate(steps):
        # Check if user stopped
        try:
            if _continuous_action.get("stop_flag") and _continuous_action["stop_flag"].is_set():
                speak("Plan cancelled")
                return
        except Exception:
            pass
        
        if step["type"] == "wait":
            secs = step["seconds"]
            print(f"  [Plan] Waiting {secs}s...")
            time.sleep(secs)
            continue
        
        if step["type"] == "confirm":
            msg = step.get("message", "Should I continue?")
            speak(msg)
            response = listen(timeout=8)
            if response:
                r = response.lower()
                if any(w in r for w in ["no", "stop", "cancel", "quit", "abort", "don't"]):
                    speak("Stopping here")
                    return
                if any(w in r for w in ["skip", "next", "pass"]):
                    speak("Skipping")
                    continue
            # Default: continue
            continue
        
        if step["type"] == "command":
            cmd_num += 1
            cmd = step["command"]
            print(f"\n  [Plan Step {cmd_num}/{total}] {cmd}")
            
            # Parse and execute the step
            try:
                intent = parse_command(cmd, use_fallback=False)
                action = intent.get("action", "")
                
                # Check for unknown/failed parse
                if action == "unknown":
                    speak(f"Step {cmd_num} unclear: {cmd}. Skip or retry?")
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
                    # Default: skip
                    continue
                
                # Execute the step
                execute(intent)
                
                # Brief pause between steps
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


# ---- Local planner (instant, no LLM needed for common patterns) -----------

def plan_locally(text):
    """
    Try to plan steps WITHOUT calling Ollama.
    Handles common multi-step patterns directly.
    Returns list of steps or None.
    """
    t = normalize_command(text)
    steps = []
    ctx = get_context()
    
    # ---- Pattern: "open X in Y in Z" (file explorer navigation) ----
    # "open jersey in movies in file explorer"
    # "open resume in documents"
    # "open song.mp3 in music in D drive"
    
    folder_match = re.match(
        r"(?:open|find|go to|navigate to)\s+(.+?)\s+in\s+(.+)", t)
    if folder_match:
        target = folder_match.group(1).strip()
        path = folder_match.group(2).strip()
        
        # Split path by "in" to get folder chain
        parts = [p.strip() for p in path.split(" in ")]
        parts = [p for p in parts if p]  # remove empty
        
        # Remove "file explorer" / "explorer" from parts if present
        need_open_explorer = False
        cleaned_parts = []
        for p in parts:
            if p in ("file explorer", "explorer", "my computer", "this pc", "files"):
                need_open_explorer = True
            else:
                cleaned_parts.append(p)
        
        # If no "file explorer" mentioned but we're not in explorer, open it
        if ctx != "explorer" and not need_open_explorer:
            need_open_explorer = True
        
        # Build steps
        if need_open_explorer:
            steps.append({"type": "command", "command": "open file explorer", "raw": "open file explorer"})
            steps.append({"type": "wait", "seconds": 2, "raw": "wait 2"})
        
        # Navigate through each folder
        for folder in cleaned_parts:
            folder = folder.strip().rstrip(".")
            if folder in ("folder", "directory", "drive", "disk"):
                continue
            # Clean up: "movies folder" -> "movies"
            folder = re.sub(r"\s*(folder|directory|dir)\s*$", "", folder).strip()
            if folder:
                steps.append({"type": "command", "command": f"click {folder}", "raw": f"click {folder}"})
                steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})
        
        # Final target
        target = re.sub(r"\s*(folder|directory|file)\s*$", "", target).strip()
        if target:
            steps.append({"type": "command", "command": f"click {target}", "raw": f"click {target}"})
        
        return steps if len(steps) > 1 else None
    
    # ---- Pattern: "open X and search/type Y" (browser) ----
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
    
    # ---- Pattern: "search for X on Y" (specific site) ----
    site_search = re.match(
        r"(?:search\s+for|search|find|look\s+for)\s+(.+?)\s+(?:on|in)\s+(youtube|google|amazon|github|spotify|netflix)", t)
    if site_search:
        query = site_search.group(1).strip()
        site = site_search.group(2).strip()
        
        site_urls = {
            "youtube": "youtube.com",
            "google": "google.com",
            "amazon": "amazon.com",
            "github": "github.com",
            "spotify": "open.spotify.com",
            "netflix": "netflix.com",
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
        
        # Find and click search box, then type query
        if site == "youtube":
            steps.append({"type": "command", "command": "click search", "raw": "click search"})
        else:
            steps.append({"type": "command", "command": "press ctrl l", "raw": "press ctrl l"})
        steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})
        steps.append({"type": "command", "command": f"type {query}", "raw": f"type {query}"})
        steps.append({"type": "command", "command": "press enter", "raw": "press enter"})
        
        return steps if len(steps) > 1 else None
    
    # ---- Pattern: "X then Y then Z" / "X and then Y" ----
    if re.search(r"\b(then|and then|after that|afterwards|next)\b", t):
        # Split by connectors
        parts = re.split(r"\b(?:then|and then|after that|afterwards|next|,\s*then)\b", t)
        parts = [p.strip().rstrip(".").strip() for p in parts if p.strip()]
        
        if len(parts) >= 2:
            for p in parts:
                if p:
                    steps.append({"type": "command", "command": p, "raw": p})
                    steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})
            return steps if len(steps) > 1 else None
    
    # ---- Pattern: "X and Y and Z" (multiple actions) ----
    if re.search(r"(open|close|minimize|maximize|click|launch)\s+.+\s+and\s+(open|close|minimize|maximize|click|launch)", t):
        parts = re.split(r"\s+and\s+", t)
        parts = [p.strip() for p in parts if p.strip()]
        
        if len(parts) >= 2:
            for p in parts:
                steps.append({"type": "command", "command": p, "raw": p})
                steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})
            return steps if len(steps) > 1 else None
    
    # ---- Pattern: "go to X folder" (in explorer) ----
    go_folder = re.match(
        r"(?:go to|navigate to|open)\s+(?:the\s+)?(.+?)\s+folder", t)
    if go_folder and ctx == "explorer":
        folder = go_folder.group(1).strip()
        # Split if multiple folders: "documents then projects"
        folders = re.split(r"\s+(?:then|and then|>|/|\\)\s+", folder)
        for f in folders:
            f = f.strip()
            if f:
                steps.append({"type": "command", "command": f"click {f}", "raw": f"click {f}"})
                steps.append({"type": "wait", "seconds": 1, "raw": "wait 1"})
        return steps if len(steps) > 1 else None
    
    return None


def parse_command(text, use_fallback=True):
    debug_log(f"parse_command input: '{text}'")
    t = normalize_command(text)
    debug_log(f"after normalize: '{t}'")

    # ---- Multi-step command detection (FIRST PRIORITY) ----
    if use_fallback and is_multi_step_command(text):
        print(f"  [Multi-Step] Detected complex command: \"{text}\"")
        
        # Try local planner first (instant, no LLM)
        local_plan = plan_locally(text)
        if local_plan:
            print(f"  [Local Plan] {len(local_plan)} steps")
            for s in local_plan:
                print(f"    -> {s['raw']}")
            return {"action": "execute_plan", "target": text, "steps": local_plan}
        
        # Fall back to Ollama planner
        if ollama_ok():
            ollama_plan = plan_steps_with_ollama(text)
            if ollama_plan:
                print(f"  [Ollama Plan] {len(ollama_plan)} steps")
                for s in ollama_plan:
                    print(f"    -> {s['raw']}")
                return {"action": "execute_plan", "target": text, "steps": ollama_plan}
        
        # If both fail, try to at least extract the main action
        print(f"  [Multi-Step] Could not plan, trying as single command")

    # Show learned translations
    if t in ("show learned", "list translations", "what have you learned"):
        return {"action": "show_translations", "target": ""}

    # ---- STOP command (highest priority) ----
    if is_stop_command(t):
        return {"action": "stop_continuous", "target": ""}

    # ---- Continuous scroll ("scroll down until I say stop") ----
    continuous_scroll_match = re.search(
        r"(?:keep\s+)?scroll(?:ing)?\s+(up|down)\s+"
        r"(?:until|till|while|and keep going|continuously|forever|non-?stop)",
        t)
    if continuous_scroll_match:
        direction = continuous_scroll_match.group(1)
        speed = "normal"
        if re.search(r"(slow|slowly)", t):
            speed = "slow"
        elif re.search(r"(fast|quickly|quick)", t):
            speed = "fast"
        return {"action": "continuous_scroll", "target": direction, "speed": speed}

    # "keep scrolling up/down"
    m = re.search(r"keep\s+scroll(?:ing)?\s+(up|down)", t)
    if m:
        speed = "normal"
        if re.search(r"(slow|slowly)", t):
            speed = "slow"
        elif re.search(r"(fast|quickly|quick)", t):
            speed = "fast"
        return {"action": "continuous_scroll", "target": m.group(1), "speed": speed}

    # "scroll up/down forever/continuously"
    m = re.search(r"scroll\s+(up|down)\s+(forever|continuously|non-?stop|always)", t)
    if m:
        return {"action": "continuous_scroll", "target": m.group(1), "speed": "normal"}

    # ---- Continuous mouse move ----
    # "move right until I say stop" / "keep moving down"
    continuous_move_match = re.search(
        r"(?:keep\s+)?(?:move|go|moving|going)\s+"
        r"(up right|upper right|up left|upper left|"
        r"down right|lower right|down left|lower left|"
        r"right|left|up|down)\s+"
        r"(?:until|till|while|and keep going|continuously|forever|non-?stop)",
        t)
    if continuous_move_match:
        direction = continuous_move_match.group(1)
        speed = "normal"
        if re.search(r"(slow|slowly)", t):
            speed = "slow"
        elif re.search(r"(fast|quickly|quick)", t):
            speed = "fast"
        return {"action": "continuous_move", "target": direction, "speed": speed}

    # "keep moving right"
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

    # "move right forever" / "move down continuously"
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
    if re.search(r"(?:open|launch|start)\s+(?:the\s+)?(\w+)\s+(?:chrome\s+)?account",
                  t):
        m2 = re.search(r"(?:open|launch|start)\s+(?:the\s+)?(\w+)\s+(?:chrome\s+)?account", t)
        word = m2.group(1)
        num = ORDINALS.get(word) or (int(word) if word.isdigit() else None)
        if num:
            return {"action": "chrome_account", "target": str(num)}

    # ---- Tab switching by NUMBER ----
    # "go to tab 4" / "open tab 3" / "switch to tab 2" / "tab 5"
    # MUST come before "go to" mouse movement and before "open" app parsing
    tab_num_match = re.search(
        r"(?:go to|switch to|open|jump to|change to|move to)?\s*tab\s+(\w+)", t)
    if tab_num_match:
        word = tab_num_match.group(1).strip()
        num = ORDINALS.get(word) or (int(word) if word.isdigit() else None)
        if num and 1 <= num <= 9:
            return {"action": "switch_tab_number", "target": str(num)}
        # If not a number, it's a tab name like "go to tab gmail"
        if word and not word.isdigit() and word not in ORDINALS:
            return {"action": "click_tab", "target": word}

    # "go to 3rd tab" / "open 2nd tab" / "switch to first tab"
    tab_ord_match = re.search(
        r"(?:go to|switch to|open|jump to|change to|move to)\s+(?:the\s+)?(\w+)\s+tab\b", t)
    if tab_ord_match:
        word = tab_ord_match.group(1).strip()
        num = ORDINALS.get(word) or (int(word) if word.isdigit() else None)
        if num and 1 <= num <= 9:
            return {"action": "switch_tab_number", "target": str(num)}
        # Not a number - it's a tab name like "go to gmail tab"
        if word and word not in ("the", "a", "this", "that"):
            return {"action": "click_tab", "target": word}

    # "next tab" / "previous tab"
    if re.search(r"\bnext\s+tab\b", t):
        return {"action": "next_tab", "target": ""}
    if re.search(r"\b(previous|prev|last)\s+tab\b", t):
        return {"action": "prev_tab", "target": ""}

    # "close tab" / "close 3rd tab" / "close this tab"
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

    # ---- Click tab by NAME (only if not caught above) ----
    m = re.search(
        r"(?:go to|switch to|open|click|jump to)\s+(?:the\s+)?"
        r"(.+?)\s+tab\b", t)
    if m:
        name = m.group(1).strip()
        if name not in ORDINALS and not name.isdigit():
            return {"action": "click_tab", "target": name}
    m = re.search(r"\btab\s+(?:called|named)\s+(.+)", t)
    if m:
        return {"action": "click_tab", "target": m.group(1).strip()}

    # ---- Taskbar / Window management ----

    # "close all minimized windows"
    if re.search(r"close\s+all\s+minimized", t):
        return {"action": "close_all_minimized", "target": ""}

    # "minimize all [app] windows"
    m = re.search(r"minimize\s+all\s+(\w+)", t)
    if m:
        name = m.group(1).strip()
        if name not in ("windows", "all"):
            return {"action": "minimize_window_by_name", "target": name,
                    "occurrence": "all"}

    # "maximize all [app] windows"
    m = re.search(r"maximize\s+all\s+(\w+)", t)
    if m:
        name = m.group(1).strip()
        if name not in ("windows", "all"):
            return {"action": "maximize_window_by_name", "target": name,
                    "occurrence": "all"}

    # "minimize all" / "show desktop" / "hide all windows"
    if re.search(r"\b(minimize all|show desktop|hide all|hide everything)\b", t):
        return {"action": "minimize_all", "target": ""}

    # "maximize all windows"
    if re.search(r"\bmaximize all\b", t):
        return {"action": "maximize_all", "target": ""}

    # "restore all" / "restore all windows" / "bring back all"
    if re.search(r"(restore all|bring back all|unminimize all|show all)", t):
        return {"action": "restore_all_minimized", "target": ""}

    # "list windows" / "what's open"
    if re.search(r"(list windows|what.?s open|show windows|open windows)", t):
        return {"action": "list_windows", "target": ""}

    # ---- CLOSE windows by name ----
    # "close all chrome windows"
    m = re.search(r"close\s+all\s+(\w+)(?:\s+windows?)?", t)
    if m:
        name = m.group(1).strip()
        if name not in ("minimized", "windows", "tabs"):
            return {"action": "close_window_by_name", "target": name,
                    "occurrence": "all"}

    # "close 2nd chrome" / "close second chrome window"
    m = re.search(r"close\s+(?:the\s+)?(\w+)\s+(\w+?)(?:\s+window)?$", t)
    if m:
        ord_word = m.group(1)
        name = m.group(2).strip()
        num = ORDINALS.get(ord_word) or (int(ord_word) if ord_word.isdigit() else None)
        if num and (name in APP_MAP or (len(name) > 2 and name not in ("window", "tab", "this"))):
            return {"action": "close_window_by_name", "target": name,
                    "occurrence": num}

    # ---- MINIMIZE windows ----
    # "minimize all chrome windows"
    m = re.search(r"minimize\s+all\s+(\w+)", t)
    if m:
        name = m.group(1).strip()
        if name not in ("windows", "all"):
            return {"action": "minimize_window_by_name", "target": name,
                    "occurrence": "all"}

    # "minimize this" / "minimize this window" / "minimize current"
    if re.search(r"minimize\s+(this|current|it|now)(\s+window)?$", t) or t.strip() == "minimize":
        return {"action": "minimize_current", "target": ""}

    # "minimize chrome" / "minimize 2nd chrome"
    m = re.search(r"minimize\s+(?:the\s+)?(?:(\w+)\s+)?(\w+)(?:\s+window)?$", t)
    if m and "all" not in t:
        ord_word, name = m.group(1), m.group(2)
        if name in APP_MAP or (name and name not in ("window", "this", "it", "all", "current", "now")):
            num = 1
            if ord_word:
                num = ORDINALS.get(ord_word) or (int(ord_word) if ord_word.isdigit() else 1)
            return {"action": "minimize_window_by_name", "target": name,
                    "occurrence": num}

    # ---- MAXIMIZE windows ----
    # "maximize all chrome windows"
    m = re.search(r"maximize\s+all\s+(\w+)", t)
    if m:
        name = m.group(1).strip()
        if name not in ("windows", "all"):
            return {"action": "maximize_window_by_name", "target": name,
                    "occurrence": "all"}

    # "maximize this" / "maximize current" / "fullscreen this window"
    if re.search(r"maximize\s+(this|current|it|now)(\s+window)?$", t) or t.strip() == "maximize":
        return {"action": "maximize_current", "target": ""}

    # "maximize chrome" / "maximize 2nd vlc"
    m = re.search(r"maximize\s+(?:the\s+)?(?:(\w+)\s+)?(\w+)(?:\s+window)?$", t)
    if m and "all" not in t:
        ord_word, name = m.group(1), m.group(2)
        if name in APP_MAP or (name and name not in ("window", "this", "it", "all", "current", "now")):
            num = 1
            if ord_word:
                num = ORDINALS.get(ord_word) or (int(ord_word) if ord_word.isdigit() else 1)
            return {"action": "maximize_window_by_name", "target": name,
                    "occurrence": num}

    # ---- RESTORE windows ----
    # "restore this" / "restore current window" / "un-maximize"
    if re.search(r"(restore|un-?maximize|unmaximize)\s+(this|current|it|now)(\s+window)?$", t) or t.strip() in ("restore", "unmaximize", "un-maximize"):
        return {"action": "restore_current", "target": ""}

    # "restore chrome" / "bring back chrome" / "open minimized chrome"
    m = re.search(r"(?:restore|bring back|unminimize|open minimized)\s+(?:the\s+)?(?:(\w+)\s+)?(\w+)", t)
    if m:
        ord_word, name = m.group(1), m.group(2)
        if name and name not in ("all", "windows"):
            num = 1
            if ord_word:
                num = ORDINALS.get(ord_word) or (int(ord_word) if ord_word.isdigit() else 1)
            return {"action": "restore_window", "target": name, "occurrence": num}

    # "switch to chrome" / "focus chrome" / "activate chrome"
    m = re.search(r"(?:switch to|focus|activate|bring up)\s+(?:the\s+)?(?:(\w+)\s+)?(\w+)(?:\s+window)?", t)
    if m and "tab" not in t:
        ord_word, name = m.group(1), m.group(2)
        if name in APP_MAP:
            num = 1
            if ord_word:
                num = ORDINALS.get(ord_word) or (int(ord_word) if ord_word.isdigit() else 1)
            return {"action": "focus_window", "target": name, "occurrence": num}

    # ---- Smart Open (context-aware) ----
    # "open chrome" -> launches Chrome
    # "open youtube" -> opens youtube.com in browser
    # "open my downloads folder" (in Explorer) -> searches Explorer
    # "open [anything else]" -> smart context handling
    for pat in [r"(?:please\s+)?(?:can you\s+)?(?:open|launch|start|run)\s+(.+)"]:
        m = re.match(pat, t)
        if m:
            target = m.group(1).strip().rstrip(".")
            # Skip tab commands (handled above)
            if re.match(r"tab\s+\w+", target) or re.match(r"(?:the\s+)?\w+\s+tab$", target):
                continue
            # Skip account commands (handled above)
            if "account" in target and "chrome" in target:
                continue
            return {"action": "smart_open", "target": target}

    # Bare app name
    if t in APP_MAP:
        return {"action": "open_app", "target": t}

    # ---- Play / Pause ----
    if re.search(r"\b(play|pause|resume|stop playing)\b", t):
        return {"action": "play_pause", "target": ""}

    # ---- Volume ----
    if re.search(r"(volume up|increase volume|louder|turn.* up)", t):
        amount = parse_number_words(t) or 5
        return {"action": "volume_up", "target": str(amount)}
    if re.search(r"(volume down|decrease volume|quieter|softer|turn.* down)", t):
        amount = parse_number_words(t) or 5
        return {"action": "volume_down", "target": str(amount)}
    if re.search(r"(mute|unmute)", t):
        return {"action": "mute", "target": ""}

    # ---- Backward FIRST (so "skip back" doesn't trigger forward) ----
    if re.search(r"(rewind|backward|skip back|go back(ward)?|previous|back\s+\d)", t):
        secs = parse_number_words(t) or 10
        return {"action": "backward", "target": str(secs)}
    # ---- Forward ----
    if re.search(r"(fast.?forward|skip forward|skip ahead|forward|skip)", t):
        secs = parse_number_words(t) or 10
        return {"action": "forward", "target": str(secs)}

    # ---- Browser back (NOT "go down/up/left/right") ----
    # Only match "go back" / "browser back", NOT "go down"
    if re.search(r"\bbrowser\s*back\b", t) or t.strip() == "back":
        return {"action": "go_back", "target": ""}
    if re.search(r"\bgo\s+back\b", t):
        # Make sure it's NOT "go back" in the context of rewind (already caught)
        return {"action": "go_back", "target": ""}

    # ---- Fullscreen ----
    if re.search(r"(full.?screen|enter full|exit full)", t):
        return {"action": "fullscreen", "target": ""}

    # ---- Scroll (STRICT - must have scroll word AND direction) ----
    # Must explicitly say "scroll up" or "scroll down" - NEVER default
    scroll_match = re.search(r"\bscroll(?:ing)?\s+(up|down|top|bottom)\b", t)
    if scroll_match:
        direction = scroll_match.group(1)
        
        # Determine amount
        scroll_amount = 10
        n = parse_number_words(t)
        if n and n < 100:  # Sanity check - don't scroll 2024 lines
            scroll_amount = n * 2
        
        if re.search(r"\b(a lot|much|way|lot|lots)\b", t):
            scroll_amount = 40
        elif re.search(r"\b(little|bit|slightly|small|tiny)\b", t):
            scroll_amount = 4
        elif re.search(r"\b(halfway|half way|half)\b", t):
            scroll_amount = 15
        
        if direction in ("up", "top"):
            return {"action": "scroll_up", "target": str(scroll_amount)}
        else:
            return {"action": "scroll_down", "target": str(scroll_amount)}
    
    # Alternative scroll patterns (strict - must have direction word)
    # "page down" / "page up"
    if re.search(r"\bpage\s+down\b", t) and "search" not in t and "find" not in t:
        return {"action": "scroll_down", "target": "15"}
    if re.search(r"\bpage\s+up\b", t) and "search" not in t and "find" not in t:
        return {"action": "scroll_up", "target": "15"}
    
    # "scroll to top/bottom"
    if re.search(r"scroll\s+to\s+(?:the\s+)?top", t):
        return {"action": "scroll_up", "target": "100"}
    if re.search(r"scroll\s+to\s+(?:the\s+)?bottom", t):
        return {"action": "scroll_down", "target": "100"}
    
    # If "scroll" is said BUT without direction - ask for clarification
    # DO NOT default to scrolling down
    if re.search(r"\bscroll\b", t) and not re.search(r"(up|down|top|bottom|left|right)", t):
        return {"action": "ask_scroll_direction", "target": ""}

    # ---- Mouse movement ----
    # "go down" / "go up" / "move right" / "cursor left" = MOVE MOUSE
    # Modifiers: "half" / "a lot" / "a little" / "by 300"
    move_match = re.search(
        r"\b(?:move|go|cursor|mouse|come|shift|slide)\s+"
        r"(?:to\s+)?(?:the\s+)?"
        r"(up right|upper right|up left|upper left|"
        r"down right|lower right|down left|lower left|"
        r"right|left|up|down)"
        r"(?:\s+(?:by\s+)?(\d+)(?:\s*(?:px|pixels?))?)?",
        t)
    if move_match:
        direction = move_match.group(1)
        pixels = move_match.group(2)

        if pixels:
            # Explicit pixel amount: "move right by 300"
            amount = int(pixels)
        else:
            # Default amount
            amount = 200

            # Size modifiers (check in priority order)
            if re.search(r"\b(half|halfway|half\s*way)\b", t):
                amount = 100  # Half of default 200
            elif re.search(r"\b(quarter|little bit|tiny bit|small bit)\b", t):
                amount = 50
            elif re.search(r"\b(a lot|much more|way more|very far|really far|far)\b", t):
                amount = 500
            elif re.search(r"\b(double|twice|two times)\b", t):
                amount = 400
            elif re.search(r"\b(little|bit|slightly|small|tiny|short)\b", t):
                amount = 80
            elif re.search(r"\b(a lot|much|more|way|big|lot)\b", t):
                amount = 400

            # Number without "pixels" keyword: "move right 300"
            n = parse_number_words(t)
            if n and n > 20:
                amount = n

        return {"action": "move_mouse", "target": direction,
                "amount": amount}

    # Double click here (check before single click!)
    if re.search(r"\bdouble\s*click\s+(here|now|there|this)?\b", t):
        return {"action": "double_click_here", "target": ""}
    if t.strip() in ("double click", "doubleclick", "double tap"):
        return {"action": "double_click_here", "target": ""}

    # Right click here (check before single click!)
    if re.search(r"\bright\s*click\s+(here|now|there|this)?\b", t):
        return {"action": "right_click_here", "target": ""}
    if t.strip() in ("right click", "rightclick"):
        return {"action": "right_click_here", "target": ""}

    # ---- Click here / click at cursor ----
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

    # ---- Key press / Key combos ----
    # "press ctrl c" / "press control shift s" / "hit alt f4"
    # MUST come before generic click parsing
    
    # "hold shift for 3 seconds"
    hold_match = re.search(
        r"hold\s+(?:down\s+)?(?:the\s+)?(.+?)(?:\s+for\s+(\d+)\s*(?:seconds?|secs?)?)?$", t)
    if hold_match:
        key = hold_match.group(1).strip()
        duration = int(hold_match.group(2)) if hold_match.group(2) else 2
        return {"action": "hold_key", "target": key, "duration": duration}
    
    # "press ctrl c 3 times" / "hit enter 5 times"
    repeat_match = re.search(
        r"(?:press|hit|tap|push)\s+(?:the\s+)?(.+?)\s+(\d+)\s+times?", t)
    if repeat_match:
        combo = repeat_match.group(1).strip()
        times = int(repeat_match.group(2))
        # Make sure it's not a click command
        if not re.search(r"\b(on|button|word|text|link)\b", combo):
            return {"action": "press_key_repeat", "target": combo, "times": times}
    
    # "press ctrl shift s" / "press win x" / "hit alt f4" / "press c"
    key_match = re.search(
        r"(?:press|hit|tap|push|type)\s+(?:the\s+)?(?:key\s+)?(.+)", t)
    if key_match:
        combo = key_match.group(1).strip().rstrip(".")
        
        # Check if it's actually a key/combo, not a click command
        # Skip if combo contains words like "on", "button", "the button"
        if re.search(r"\b(on|button|word|text|link|icon|image)\b", combo):
            pass  # Let it fall through to click parsing
        else:
            # Validate: at least one part should be a recognized key
            test_keys = parse_key_combo(combo)
            is_valid_key = False
            for k in test_keys:
                k_lower = k.lower()
                if (k_lower in KEY_NAME_MAP or
                    k_lower in KEY_NAME_MAP.values() or
                    k_lower in MODIFIER_MAP or
                    k_lower in MODIFIER_MAP.values() or
                    (len(k_lower) == 1 and (k_lower.isalpha() or k_lower.isdigit())) or
                    re.match(r"f\d+", k_lower)):
                    is_valid_key = True
                    break
            
            if is_valid_key:
                return {"action": "press_key_combo", "target": combo}
    
    # Bare key names: "enter" / "escape" / "space" / "f5"
    bare_key = t.strip()
    if bare_key in KEY_NAME_MAP and bare_key not in ("tab",):
        # "tab" is handled separately for browser tab switching
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
                target = groups[1].strip().rstrip(".")
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
    m = re.match(r"(?:type|write|enter)\s+(.+)", t)
    if m:
        return {"action": "type_text", "target": m.group(1).strip()}

    # ---- Search (context-aware) ----
    # "search for cats on youtube"
    m = re.match(r"(?:search|find|look\s+up)\s+(?:for\s+)?(.+?)\s+on\s+youtube", t)
    if m:
        return {"action": "search_youtube", "target": m.group(1).strip()}
    
    # "search youtube for cats"
    m = re.match(r"(?:search|find)\s+youtube\s+for\s+(.+)", t)
    if m:
        return {"action": "search_youtube", "target": m.group(1).strip()}
    
    # "google xyz" -> always Google in browser
    m = re.match(r"google\s+(?:for\s+)?(.+)", t)
    if m:
        return {"action": "google_search", "target": m.group(1).strip()}
    
    # "search for X" / "look up X" / "find X" -> context-aware
    m = re.match(r"(?:search|find|look\s+up)\s+(?:for\s+)?(.+)", t)
    if m:
        query = m.group(1).strip()
        if not re.match(r"tab\s+\d", query):
            return {"action": "smart_search", "target": query}

    # ---- Window ----
    if "close" in t and ("this" in t or "window" in t):
        return {"action": "close_app", "target": ""}
    if "minimize" in t:     return {"action": "minimize_current", "target": ""}
    if "maximize" in t:     return {"action": "maximize_current", "target": ""}
    if "screenshot" in t:   return {"action": "screenshot", "target": ""}
    if "new tab" in t:      return {"action": "new_tab", "target": ""}

    # Expanded bare words:
    if t.strip() in ("enter", "confirm", "ok", "okay", "submit"):
        return {"action": "press_key_combo", "target": "enter"}
    if t.strip() in ("escape", "cancel", "dismiss"):
        return {"action": "press_key_combo", "target": "escape"}
    if t.strip() == "tab":
        return {"action": "press_key_combo", "target": "tab"}

    # ---- FALLBACK: Ask Ollama to translate ----
    if use_fallback and ollama_ok():
        translated = ollama_translate_command(text)
        
        if translated and translated.strip() != text.lower().strip():
            # VALIDATION: Reject bad translations
            bad_translations = ["unknown", "scroll", "move", "click", 
                                "press", "open", "nothing", "error",
                                "none", "", " "]
            if translated.strip() in bad_translations:
                print(f"  [Fallback] Ollama returned unclear '{translated}' - ignoring")
            else:
                # Check for incomplete scroll commands
                if translated.strip() == "scroll" or re.match(r"^scroll\s*$", translated):
                    print(f"  [Fallback] Rejected incomplete scroll command")
                else:
                    print(f"  [Fallback] Re-parsing: \"{translated}\"")
                    result = parse_command(translated, use_fallback=False)
                    # Only use if it's a valid specific action
                    if result.get("action") not in ("click_word",):
                        return result
                    # If it fell through to click_word, verify the target makes sense
                    if result.get("target") and len(result.get("target", "")) > 2:
                        return result
        
        # Secondary: keyword extraction (but validate)
        kw = extract_keyword(t)
        if kw and kw != text.lower().strip() and len(kw) > 1:
            # Reject if keyword is a direction word (would cause scroll bug)
            direction_words = ["up", "down", "left", "right", "top", "bottom",
                              "scroll", "move", "page"]
            if kw.lower() not in direction_words:
                if kw in APP_MAP:
                    return {"action": "open_app", "target": kw}
                return {"action": "click_word", "target": kw, "occurrence": 1}

    # Last resort: treat as click target (ONLY if meaningful)
    if len(t.strip()) > 2 and not re.match(r"^(the|a|an|is|am|are|was|were)$", t.strip()):
        return {"action": "click_word", "target": t, "occurrence": 1}
    
    # Nothing matched - do nothing instead of defaulting to scroll
    return {"action": "unknown", "target": text}


# ---- Actions ----------------------------------------------------------------

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
    pyautogui.hotkey("win")
    time.sleep(1.2)
    pyautogui.typewrite(name, interval=0.04)
    time.sleep(2.0)
    pyautogui.press("enter")
    time.sleep(0.5)
    speak("Done.")


def open_chrome_account(num=None):
    """Open Chrome. If an account picker appears, click the Nth account."""
    speak("Opening Chrome")
    try:
        subprocess.Popen(f'start "" "chrome"', shell=True)
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
    # Wait for picker to appear (up to 5s)
    _ocr_cache["words"] = None  # force fresh OCR
    found = None
    for _ in range(10):
        time.sleep(0.5)
        words = ocr_screen(force=True)
        # Look for account picker keywords
        picker_markers = ["choose", "profile", "who", "using", "account",
                          "select", "gmail.com", "@"]
        has_picker = any(any(mk in w["text"] for mk in picker_markers)
                          for w in words)
        if has_picker:
            found = words
            break

    if not found:
        print("  [Chrome] No account picker detected.")
        return

    # Account tiles are usually in a horizontal row. Group by Y, pick row with
    # most text items, then pick the Nth from left.
    found.sort(key=lambda w: (w["y"], w["x"]))

    # Find clickable account tiles - usually names + @gmail.com text
    emails = [w for w in found if "@" in w["raw"] or "gmail" in w["text"]]
    if emails:
        emails.sort(key=lambda w: w["x"])
        if num <= len(emails):
            pick = emails[num - 1]
            print(f"  [Chrome] Clicking account #{num}: {pick['raw']}")
            smart_click(pick["x"], pick["y"])
            return

    # Fallback: group all tiles in central horizontal band
    screen_h = pyautogui.size()[1]
    center_band = [w for w in found
                   if screen_h * 0.25 < w["y"] < screen_h * 0.75]
    center_band.sort(key=lambda w: w["x"])
    if num <= len(center_band):
        pick = center_band[num - 1]
        smart_click(pick["x"], pick["y"])
    else:
        speak("Could not find that account")


def click_tab_by_name(name):
    """Find Chrome/browser tab by name and click it."""
    speak(f"Switching to {name} tab")
    words = ocr_screen(force=True)
    if not words:
        speak("Can't read screen")
        return

    screen_w, screen_h = pyautogui.size()
    # Tabs live in the TOP ~80px strip of the browser
    tab_strip = [w for w in words if w["y"] < 90]
    if not tab_strip:
        tab_strip = [w for w in words if w["y"] < screen_h * 0.1]

    name_low = name.lower().strip()
    name_parts = name_low.split()

    # Score each tab-strip word
    best = None
    best_score = 0

    # Single word match
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

    # Multi-word match within tab strip
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
        # Last resort: search anywhere on screen (maybe tab bar is lower)
        c = find_on_screen(name, prefer_region="top")
        if c:
            reliable_click(c[0], c[1])
        else:
            speak(f"No tab named {name}")


def click_word(word, occurrence=1):
    speak(f"Clicking {word}")
    coords = find_on_screen(word, occurrence=occurrence, prefer_region="center")
    if coords:
        smart_click(coords[0], coords[1])
        return
    hw_scroll(-5); time.sleep(0.4)
    coords = find_on_screen(word, occurrence=occurrence)
    if coords:
        smart_click(coords[0], coords[1])
        return
    hw_scroll(5)
    if ollama_ok():
        kw = extract_keyword(f"click {word}")
        if kw and kw != word:
            c2 = find_on_screen(kw)
            if c2:
                smart_click(c2[0], c2[1])
                return
    speak(f"Can't find {word}")


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
    # write() handles unicode better than typewrite() for extended ASCII
    try:
        pyautogui.write(text, interval=0.03)
    except Exception:
        pyautogui.typewrite(text, interval=0.03)


def do_search(query):
    speak(f"Searching for {query}")
    import webbrowser
    # URL-encode the query
    webbrowser.open(f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}")


def show_translations():
    if not _translation_cache:
        speak("Nothing learned yet")
        return
    print("\n  === Learned Translations ===")
    for user, cmd in _translation_cache.items():
        print(f"  \"{user}\" -> \"{cmd}\"")
    print()
    speak(f"I've learned {len(_translation_cache)} translations")


# ---- Media (uses direct VK codes - works everywhere) ----------------------
def play_pause():
    speak("Play pause")
    press_vk(VK_MEDIA_PLAY)

def volume_up(amount=5):
    speak(f"Volume up {amount}")
    press_vk(VK_VOLUME_UP, times=int(amount), delay=0.08)

def volume_down(amount=5):
    speak(f"Volume down {amount}")
    press_vk(VK_VOLUME_DOWN, times=int(amount), delay=0.08)

def mute_toggle():
    speak("Mute toggle")
    press_vk(VK_VOLUME_MUTE)

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
    """Context-aware fullscreen toggle."""
    ctx = get_context()
    cls, title, proc = get_foreground_window_info()
    
    # YouTube in browser: press F
    if ctx == "browser" and "youtube" in title:
        speak("Fullscreen")
        pyautogui.press("f")
        return
    
    # VLC: press F
    if "vlc" in proc:
        speak("Fullscreen")
        pyautogui.press("f")
        return
    
    # Media players that use F for fullscreen
    media_procs = ("vlc.exe", "mpv.exe", "mpc-hc.exe", "mpc-hc64.exe",
                   "potplayer.exe", "potplayermini.exe", "potplayermini64.exe")
    if any(mp in proc for mp in media_procs):
        speak("Fullscreen")
        pyautogui.press("f")
        return
    
    # Default: F11 (browsers, most apps)
    speak("Fullscreen")
    pyautogui.press("f11")

# ---- Scroll (BIG - uses mouse wheel directly) -----------------------------
def scroll_up(amount=5):
    """amount of 5 = ~half a screen, 15 = several screens."""
    speak("Scrolling up")
    # Each "click" = one wheel notch = ~3 lines. Multiply for noticeable movement.
    hw_scroll(int(amount) * 3)

def scroll_down(amount=5):
    speak("Scrolling down")
    hw_scroll(-int(amount) * 3)

def ask_scroll_direction():
    """Ask user which direction to scroll."""
    speak("Scroll up or down?")

def handle_unknown(text=""):
    """Handle commands that couldn't be parsed."""
    print(f"  [Unknown] Could not understand: '{text}'")
    speak("I didn't catch that. Please repeat.")

# Navigation
def go_back():      speak("Back");          pyautogui.hotkey("alt", "left")
def close_win():    speak("Closing");       pyautogui.hotkey("alt", "F4")
def close_tab():    speak("Closed tab");    pyautogui.hotkey("ctrl", "w")
def new_tab():      speak("New tab");       pyautogui.hotkey("ctrl", "t")

def minimize_win():
    speak("Minimizing")
    # Press twice: first restores, second minimizes
    pyautogui.hotkey("win", "down")
    time.sleep(0.1)
    pyautogui.hotkey("win", "down")

def maximize_win(): speak("Maximized");     pyautogui.hotkey("win", "up")
def press_key(key): press_key_combo(key)

def screenshot():
    ts = time.strftime("%Y%m%d_%H%M%S")
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"screenshot_{ts}.png")
    try:
        pyautogui.screenshot().save(p)
        speak("Screenshot saved.")
    except Exception as e:
        print(f"  [!] Screenshot error: {e}")
        speak("Could not take screenshot.")


# ---- Universal Key Press System -------------------------------------------

# Map spoken key names to pyautogui key names
KEY_NAME_MAP = {
    # Letters (a-z handled automatically)
    # Numbers (0-9 handled automatically)
    
    # Special keys
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
    
    # Arrow keys
    "up": "up", "up arrow": "up", "arrow up": "up",
    "down": "down", "down arrow": "down", "arrow down": "down",
    "left": "left", "left arrow": "left", "arrow left": "left",
    "right": "right", "right arrow": "right", "arrow right": "right",
    
    # Function keys
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4",
    "f5": "f5", "f6": "f6", "f7": "f7", "f8": "f8",
    "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
    
    # Modifier keys (standalone press)
    "shift": "shift", "left shift": "shiftleft", "right shift": "shiftright",
    "control": "ctrl", "ctrl": "ctrl", "left control": "ctrlleft",
    "alt": "alt", "left alt": "altleft", "right alt": "altright",
    "windows": "win", "win": "win", "super": "win",
    "caps lock": "capslock", "caps": "capslock",
    "num lock": "numlock", "scroll lock": "scrolllock",
    "print screen": "printscreen", "prtsc": "printscreen",
    
    # Punctuation
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

# Modifier key aliases
MODIFIER_MAP = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt",
    "shift": "shift",
    "win": "win", "windows": "win", "super": "win", "command": "win",
    "fn": "fn",
}


def resolve_key_name(spoken):
    """Convert spoken key name to pyautogui key name."""
    s = spoken.lower().strip()
    
    # Direct map
    if s in KEY_NAME_MAP:
        return KEY_NAME_MAP[s]
    
    # Single letter
    if len(s) == 1 and s.isalpha():
        return s
    
    # Single digit
    if len(s) == 1 and s.isdigit():
        return s
    
    # "letter a" / "key b"
    m = re.match(r"(?:letter|key)\s+([a-z0-9])", s)
    if m:
        return m.group(1)
    
    # Function key: "function 5" -> "f5"
    m = re.match(r"function\s+(\d+)", s)
    if m:
        return f"f{m.group(1)}"
    
    return s  # Return as-is, let pyautogui handle it


def parse_key_combo(text):
    """
    Parse key combination from text.
    Returns list of keys to press simultaneously.
    
    Examples:
    - "ctrl c"          -> ["ctrl", "c"]
    - "control shift s" -> ["ctrl", "shift", "s"]
    - "win x"           -> ["win", "x"]
    - "alt f4"          -> ["alt", "f4"]
    - "c"               -> ["c"]
    """
    t = text.lower().strip()
    
    # Normalize separators: "ctrl+c", "ctrl-c", "ctrl and c"
    t = t.replace("+", " ")
    t = t.replace("-", " ")
    t = t.replace(" and ", " ")
    t = t.replace(" plus ", " ")
    t = t.replace(" with ", " ")
    
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
    
    # Modifiers first, then regular keys
    return modifiers + keys


def press_key_combo(combo_text):
    """Press a key or key combination."""
    keys = parse_key_combo(combo_text)
    if not keys:
        speak("No key recognized")
        return
    
    if len(keys) == 1:
        key = keys[0]
        speak(f"Pressing {key}")
        try:
            pyautogui.press(key)
        except Exception as e:
            print(f"  [!] Key press error: {e}")
            speak(f"Cannot press {key}")
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
    """Press a key or combo multiple times."""
    keys = parse_key_combo(combo_text)
    if not keys:
        speak("No key recognized")
        return
    
    combo_name = " + ".join(keys)
    speak(f"Pressing {combo_name} {times} times")
    
    for i in range(times):
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
    """Hold a key down for a duration."""
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


def execute(intent):
    action = intent.get("action", "")
    target = intent.get("target", "")

    # LOG every action for debugging
    print(f"  [EXECUTE] action='{action}' target='{target}' intent={intent}")

    # Handle multi-step plans separately
    if action == "execute_plan":
        steps = intent.get("steps", [])
        execute_plan(steps, target)
        return

    action_map = {
        "open_app":       lambda: open_app(target),
        "smart_open":     lambda: smart_open(target),
        "chrome_account": lambda: open_chrome_account(target),
        "click_tab":      lambda: click_tab_by_name(target),
        "switch_tab_number": lambda: switch_tab_number(target),
        "next_tab":       next_tab,
        "prev_tab":       prev_tab,
        "close_nth_tab":  lambda: close_nth_tab(target),
        # Current window
        "minimize_current":  minimize_current,
        "maximize_current":  maximize_current,
        "restore_current":   restore_current,

        # Named windows
        "close_window_by_name": lambda: close_window_by_name(target, intent.get("occurrence", 1)),
        "minimize_window_by_name": lambda: minimize_window_by_name(target, intent.get("occurrence", 1)),
        "maximize_window_by_name": lambda: maximize_window_by_name(target, intent.get("occurrence", 1)),
        "restore_window":    lambda: restore_window_by_name(target, intent.get("occurrence", 1)),

        # Bulk operations
        "minimize_all":          minimize_all_windows,
        "maximize_all":          maximize_all_windows,
        "restore_all_minimized": restore_all_minimized,
        "close_all_minimized":   close_all_minimized,
        "focus_window":   lambda: click_taskbar_icon(target, intent.get("occurrence", 1)),
        "list_windows":   list_open_windows,
        "move_mouse":     lambda: move_mouse(target, intent.get("amount", 200)),
        "click_here":        click_here,
        "double_click_here": double_click_here,
        "right_click_here":  right_click_here,
        "click_word":     lambda: click_word(target, intent.get("occurrence", 1)),
        "click_image":  lambda: click_image(target),
        "select_word":  lambda: select_word(target),
        "grid_click":   lambda: grid_click(target, intent.get("col", 1), intent.get("row")),
        "type_text":    lambda: type_text(target),
        "search":       lambda: do_search(target),
        "smart_search": lambda: smart_search(target),
        "google_search": lambda: do_search(target),
        "search_youtube": lambda: smart_search_youtube(target),
        "play_pause":   play_pause,
        "volume_up":    lambda: volume_up(int(target) if target else 5),
        "volume_down":  lambda: volume_down(int(target) if target else 5),
        "mute":         mute_toggle,
        "forward":      lambda: forward(int(target) if target else 10),
        "backward":     lambda: backward(int(target) if target else 10),
        "fullscreen":   fullscreen,
        "scroll_up":    lambda: scroll_up(int(target) if target else 5),
        "scroll_down":  lambda: scroll_down(int(target) if target else 5),
        "ask_scroll_direction": ask_scroll_direction,
        "unknown":       lambda: handle_unknown(target),
        "go_back":      go_back,
        "close_app":    close_win,
        "close_tab":    close_tab,
        "new_tab":      new_tab,
        "press_key_combo":  lambda: press_key_combo(target),
        "press_key_repeat": lambda: press_key_repeat(target, intent.get("times", 1)),
        "hold_key":         lambda: hold_key(target, intent.get("duration", 2)),
        "screenshot":   screenshot,
        "show_translations": show_translations,
        "continuous_scroll": lambda: continuous_scroll(target, intent.get("speed", "normal")),
        "continuous_move":   lambda: continuous_move(target, intent.get("speed", "normal")),
        "stop_continuous":   handle_stop,
    }

    fn = action_map.get(action)
    if fn:
        fn()
    else:
        print(f"  [!] Unknown action: '{action}'")
        speak("I didn't understand that.")


# ---- Main -------------------------------------------------------------------

def banner():
    os.system("cls" if os.name == "nt" else "clear")
    print("""
  ================================================
       ROLEX - Voice Screen Agent
  ================================================

   WAKE:     Say "Rolex" (while sleeping, wakes it up)
             "Rolex [command]" (wakes + runs command)

   SLEEP:    "Rolex sleep" / "Sleep rolex"
             "Rolex go to sleep" / "Rolex good night"
             "Rolex stop listening" / "Rolex pause"
             (MUST say Rolex - prevents accidents!)

   EXIT:     "Rolex exit" / "Exit rolex"
             "Rolex quit" / "Rolex close yourself"
             "Rolex shutdown" / "Rolex goodbye"
             "Rolex turn off"
             (MUST say Rolex - closes program)

   APPS:     "Open Chrome/VLC/Explorer/YouTube"
             "Open 2nd Chrome account"
   WINDOWS:  "Minimize" / "Maximize" / "Restore" (current)
             "Minimize chrome" / "Maximize vlc"
             "Minimize all chrome" / "Maximize all"
             "Close all chrome windows"
             "Close all minimized"
             "Restore all" / "Show desktop"
             "Switch to chrome" / "List windows"
   CLICK:    "Click" / "Click here" -> clicks at mouse position
             "Double click" / "Right click" -> at mouse position
             "Click on [word]" -> finds and clicks word
   TABS:     "Go to tab 2" / "Next tab" / "Previous tab"
             "Go to gmail tab" / "Close 3rd tab"
   IMAGES:   "Click on elephant image"
   MEDIA:    "Play" "Pause" "Volume up/down"
             "Forward" "Backward" "Fullscreen"
   FULLSCREEN: "Fullscreen" (auto-detects YouTube/VLC -> F key)
   SCROLL:   "Scroll up/down" "Scroll down a lot"
             (MUST include up or down)
   MOUSE:    "Go right" / "Go up" / "Move down" / "Go to the right"
             "Move right half" -> moves half the distance
             "Go up a little" / "Go down a lot" / "Move left 300"
   SELECT:   "Select [word]"
   GRID:     "Click 2nd video in 3rd row"
   BROWSE:   "Go back" "New tab" "Close tab"
   TYPE:     "Type [text]"
   SEARCH:   "Search for [query]" / "Search for [query] on YouTube"
   KEYS:     "Press C" / "Press F5" / "Press Enter"
             "Press Ctrl C" / "Press Win X" / "Press Alt F4"
             "Press Ctrl Shift S" (multiple modifiers)
             "Press Enter 5 times" (repeat)
             "Hold Shift for 3 seconds"
             "Copy" "Paste" "Undo" "Save" (shortcuts)
   CONTINUOUS: "Scroll down until stop" / "Keep scrolling up"
               "Move right until stop" / "Keep moving down"
               "Scroll down slowly/fast continuously"
   MULTI-STEP: (Complex commands automatically broken into steps)
               "Open jersey in movies in file explorer"
               "Search for cats on youtube in chrome"
               "Open resume in documents folder"  
               "Minimize chrome and open vlc and play"
               "Go to downloads then open first file"
               "Open chrome and search for weather"
               (Say "stop" or "cancel" during execution to halt)
               (Say "skip" to skip a confusing step)
   STOP:       "Stop" / "Enough" / "That's good"
               (works during any continuous action)
  ================================================
    """)


def test_wake_strip():
    """Debug: test wake word stripping."""
    test_cases = [
        "rolex pause",
        "rolex play",
        "rolex forward",
        "rolex volume up",
        "rolex open chrome",
        "rolex click login",
        "rolex scroll down",
        "rolex press ctrl c",
        "rolex go right",
        "rolex minimize this",
        "hey rolex open vlc",
        "rolex please open youtube",
        "pause rolex",  # trailing
    ]
    
    print("\n  === Wake Strip Tests ===")
    for test in test_cases:
        has = has_wake(test)
        stripped = strip_wake(test)
        print(f"  Input:    '{test}'")
        print(f"  Has wake: {has}")
        print(f"  Stripped: '{stripped}'")
        print(f"  Intent:   {parse_command(stripped) if stripped else 'none'}")
        print()


def main():
    banner()
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.02

    try:
        load_translations()
    except Exception:
        pass

    if ollama_ok():
        print("  [OK] Ollama running - smart mode ON")
    else:
        print("  [!] Ollama offline - basic commands work fine")
    
    try:
        if _REDIS_AVAILABLE:
            print("  [OK] Redis caching enabled")
    except Exception:
        pass
    print()

    speak("Rolex ready. Say Rolex to wake me up.")

    active = False
    
    while True:
        try:
            # ================== SLEEP MODE ==================
            if not active:
                print("\n  [SLEEP] Waiting for wake word 'Rolex'...")
                text = listen(timeout=10)
                
                if not text:
                    continue
                
                # In SLEEP mode, EVERYTHING requires "rolex" in it
                if not has_wake(text):
                    print(f"  [Ignored - need wake word] \"{text}\"")
                    continue
                
                # ---- Priority 1: Rolex exit ----
                if is_exit(text):
                    speak("Goodbye")
                    try:
                        save_translations()
                    except Exception:
                        pass
                    return
                
                # ---- Priority 2: Rolex sleep (already asleep) ----
                if is_sleep_command(text):
                    print("  [Info] Already sleeping")
                    continue
                
                # ---- Strip wake word to get command ----
                cmd = strip_wake(text)
                
                if cmd and len(cmd.strip()) >= 1:
                    # Wake word + command: WAKE UP and execute command
                    print(f"  [Wake+Command] \"{cmd}\"")
                    active = True  # ← Activate FIRST before executing
                    print("  [WAKE] Now active - executing command")
                    
                    try:
                        intent = parse_command(cmd)
                        execute(intent)
                    except Exception as e:
                        print(f"  [!] Error executing wake command: {e}")
                        speak("Error running that")
                    
                    print("  [ACTIVE] Ready for more commands")
                else:
                    # Just "Rolex" alone -> activate
                    active = True
                    speak("Yes?")
                    print("  [WAKE] Now active")

            # ================== ACTIVE MODE ==================
            else:
                print("\n  [ACTIVE] Listening for command...")
                text = listen(timeout=15)
                
                if not text:
                    print("  [...] Silence. Still active.")
                    continue
                
                # ---- PRIORITY 1: Stop continuous action (no wake word needed) ----
                try:
                    if is_stop_command(text) and _continuous_action["running"]:
                        stop_continuous_action()
                        speak("Stopped")
                        continue
                except Exception:
                    pass
                
                # ---- PRIORITY 2: EXIT (REQUIRES "rolex" word) ----
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
                
                # ---- PRIORITY 3: SLEEP (REQUIRES "rolex" word) ----
                if is_sleep_command(text):
                    try:
                        stop_continuous_action()
                    except Exception:
                        pass
                    speak("Sleeping. Say Rolex to wake me.")
                    active = False
                    print("  [SLEEP] Going to sleep")
                    continue
                
                # ---- PRIORITY 4: Strip wake word if user re-said it ----
                if has_wake(text):
                    text = strip_wake(text)
                    if not text or len(text) < 2:
                        speak("Yes?")
                        continue
                
                # ---- Execute the command (no wake word needed in active mode) ----
                intent = parse_command(text)
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


if __name__ == "__main__":
    main()
