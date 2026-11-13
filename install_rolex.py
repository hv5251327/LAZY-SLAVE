#!/usr/bin/env python3
"""Rolex installer / health bootstrapper.

Run once:
    python install_rolex.py              # core deps + checks
    python install_rolex.py --with-vosk  # also download the offline Vosk
                                         # model (~37 MB, Indian-English small)
    python install_rolex.py --check      # just print the health table, install nothing

Safe to re-run. Everything optional is reported, never fatal.
"""
import os
import sys
import subprocess
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS = os.path.join(HERE, "requirements.txt")
VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-in-0.4.zip"
VOSK_DIR = os.path.join(HERE, "vosk-model-small-en-in-0.4")

CORE = ["numpy", "Pillow", "SpeechRecognition", "sounddevice",
        "pyautogui", "pyttsx3", "requests"]
WIN_ONLY = ["pywin32"]
OPTIONAL = ["winocr", "pocketsphinx", "vosk", "redis"]


def pip_install(*packages):
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    print("  $ " + " ".join(cmd))
    try:
        subprocess.run(cmd, check=False, timeout=600)
        return True
    except Exception as e:
        print(f"  [!] pip failed: {e}")
        return False


def probe(label, mod, pip_name=None):
    try:
        __import__(mod)
        print(f"  [  ok  ] {label}")
        return True
    except Exception:
        print(f"  [MISSING] {label:44s} -> pip install {pip_name or mod}")
        return False


def check_only():
    print("\n  === Rolex health check ===")
    for label, mod in [
        ("numpy", "numpy"),
        ("Pillow", "PIL"),
        ("SpeechRecognition", "speech_recognition"),
        ("sounddevice (mic)", "sounddevice"),
        ("pyautogui", "pyautogui"),
        ("pyttsx3 (SAPI voice)", "pyttsx3"),
    ]:
        probe(label, mod)
    if sys.platform == "win32":
        probe("pywin32 (SendInput path)", "win32api")
    probe("winocr (click on text)", "winocr")
    probe("pocketsphinx (offline STT)", "pocketsphinx")
    probe("vosk (offline STT)", "vosk")
    probe("redis (cache)", "redis")
    probe("requests (Ollama LLM)", "requests")
    try:
        urllib.request.urlopen("https://www.google.com", timeout=3)
        print("  [  ok  ] internet")
    except Exception:
        print("  [MISSING] internet - voice needs it unless Vosk model is set up")
    model_ok = os.path.isdir(VOSK_DIR)
    print(f"  [{'  ok  ' if model_ok else 'MISSING'}] Vosk model at {VOSK_DIR}")
    print()


def install_core():
    print("\n  === Installing core dependencies ===")
    if not os.path.exists(REQUIREMENTS):
        pip_install(*CORE)
    else:
        pip_install("-r", REQUIREMENTS)
    if sys.platform == "win32":
        pip_install(*WIN_ONLY)


def install_vosk():
    print("\n  === Offline Vosk STT (Indian-English small model) ===")
    if not probe("vosk", "vosk"):
        pip_install("vosk")
        probe("vosk", "vosk")
    if os.path.isdir(VOSK_DIR):
        print(f"  [  ok  ] Vosk model already present: {VOSK_DIR}")
        return
    zip_path = os.path.join(HERE, "vosk-model-small-en-in-0.4.zip")
    try:
        def progress(blocks, block_size, total):
            if total > 0 and blocks % 100 == 0:
                print(f"\r  downloading... {min(100, blocks * block_size * 100 // total)}%", end="")
        urllib.request.urlretrieve(VOSK_URL, zip_path, progress)
        print()
        print("  extracting...")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(HERE)
        os.remove(zip_path)
        print(f"  [  ok  ] Vosk model ready: {VOSK_DIR}")
        print("           Rolex now transcribes offline (auto mode: Google, Vosk, pocketsphinx).")
    except Exception as e:
        print(f"  [!] Vosk model download failed: {e}")
        print("  [!] You can fetch it manually from https://alphacephei.com/vosk/models")
        print(f"  [!] and unzip to: {VOSK_DIR}")


def main():
    print("\n  ===== Rolex installer =====")
    if sys.platform != "win32":
        print("  [!] This is tuned for Windows 10/11. Continuing anyway...")

    if "--check" in sys.argv:
        check_only()
        return

    install_core()

    if "--with-optional" in sys.argv or "--all" in sys.argv:
        print("\n  === Optional dependencies ===")
        for pkg in OPTIONAL:
            pip_install(pkg)

    if "--with-vosk" in sys.argv or "--all" in sys.argv:
        install_vosk()

    # First-run: make sure the config file exists (it is auto-created on first run too)
    config = os.path.join(HERE, "rolex.config.ini")
    if not os.path.exists(config):
        print("\n  [i] rolex.config.ini will be created on first run -")
        print("      edit stt_language / stt_engine / auto_sleep_seconds there.")

    print()
    check_only()

    print("  Next steps:")
    print("    1.  python rolex_agent.py --listen     # mic + STT diagnostic")
    print("    2.  python rolex_agent.py --check      # full health table")
    print("    3.  python rolex_agent.py              # run Rolex")
    if sys.platform == "win32":
        print("\n  Windows tips:")
        print("    - sounddevice/PortAudio: bundled with the pip wheel - no separate install")
        print("    - if STT keeps failing: Settings > Time & Language > Speech,")
        print("      make sure English (India) speech pack is available")
        print("    - for click-on-text: pip install winocr (+ English OCR language pack)")


if __name__ == "__main__":
    main()