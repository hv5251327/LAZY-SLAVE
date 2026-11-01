# ROLEX - Voice-Controlled Screen & Desktop Agent

A fast, fully local voice assistant that listens for your commands, manages windows, controls media, navigates browser tabs, and clicks or types on any element visible on your screen.

---

## How It Works

1. **Say "Rolex"** (or "Hey Rolex") to wake up the agent.
2. **Rolex stays active** — once awake, you can issue continuous commands without repeating the wake word.
3. **Execute commands** — launch apps, switch tabs, manage windows, control media, or click words/images on screen.
4. **Say "Go to sleep"** to return to sleep mode, or **"Exit"** / **"Close yourself"** to shut down.

---

## Everything is LOCAL & Private

- **Windows OCR (`winocr`)**: Scans screen text natively on-device. No screenshots are sent to cloud services.
- **Local Vision (`moondream:1.8b` via Ollama)**: Local visual analysis when finding UI elements or images.
- **Local Intelligence (`qwen2.5:1.5b` via Ollama)**: Local parsing and keyword extraction for natural language.
- **Direct Win32 SendInput**: Reliable hardware-level mouse clicks, double clicks, and keyboard events.

---

## Quick Start

1. Double-click **`START_ASTRA.bat`**.
2. Wait for `Rolex ready. Say Rolex to wake me up.`
3. Say **"Rolex"** and give your commands!

---

## Voice Commands Reference

| Category | Voice Command Examples | Action |
|---|---|---|
| **Wake & Sleep** | `"Rolex"` / `"Hey Rolex"`<br>`"Go to sleep"` / `"Stop listening"`<br>`"Exit"` / `"Close yourself"` | Wake up agent<br>Return to background sleep<br>Terminate application |
| **Apps & Profiles** | `"Open Chrome"` / `"Open VLC"`<br>`"Open 2nd Chrome account"`<br>`"Open YouTube"` | Launches app or profile<br>Context-aware URL opening |
| **Tab Controls** | `"Go to tab 2"` / `"Switch to tab 4"`<br>`"Next tab"` / `"Previous tab"`<br>`"Go to gmail tab"`<br>`"Close 3rd tab"` / `"Close tab"` | Numbered tab jumping (Ctrl+1..9)<br>Cycle adjacent tabs<br>OCR click on named tab<br>Close specific tab |
| **Window Management** | `"Minimize vlc"` / `"Restore chrome"`<br>`"Close all chrome windows"`<br>`"Close all minimized windows"`<br>`"Minimize all"` / `"Show desktop"`<br>`"List windows"` | Control windows by app name<br>Batch close/restore<br>Show desktop<br>Speech list of open windows |
| **Screen Clicking** | `"Click on [word]"`<br>`"Click 2nd [word]"`<br>`"Click on elephant image"`<br>`"Click 2nd video in 3rd row"` | Local OCR word targeting<br>Target specific occurrence<br>Ollama vision detection<br>Grid spatial layout selection |
| **Media Controls** | `"Play"` / `"Pause"`<br>`"Volume up"` / `"Volume down 15"`<br>`"Mute"` / `"Unmute"`<br>`"Forward 10 seconds"`<br>`"Rewind 15 seconds"`<br>`"Fullscreen"` | Virtual key media play/pause<br>Volume steps with VK keys<br>Toggle system audio mute<br>Media seek right<br>Media seek left<br>Toggle fullscreen (F) |
| **Search & Web** | `"Search for python tutorials on youtube"`<br>`"Google how to code"`<br>`"Search for [query]"` | Searches YouTube directly<br>Google search in browser<br>Context-aware search |
| **Navigation & Typing**| `"Move mouse right 300"`<br>`"Scroll down a lot"`<br>`"Type hello world"`<br>`"Enter"` / `"Escape"` / `"Tab"` | Relative cursor movement<br>Dynamic scroll amounts<br>Keyboard typing<br>Special key presses |

---

## Requirements

- **Windows 10 / 11** (64-bit)
- **Python 3.10+** (Python 3.14 verified)
- **Microphone** (automatically detected via `sounddevice`)
- **Ollama** running locally on port 11434 with models:
  - `qwen2.5:1.5b` (for smart keyword extraction)
  - `moondream:1.8b` (for visual element detection)
