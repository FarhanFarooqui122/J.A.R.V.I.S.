"""
Jarvis - voice-controlled laptop assistant (Windows)

Pipeline:  "Hey Jarvis" -> Whisper (speech-to-text) -> Qwen (LLM -> JSON actions)
           -> Python runs the actions -> Kokoro (text-to-speech)

SETUP (run once, in a fresh virtual environment):
    pip install torch --index-url https://download.pytorch.org/whl/cu121
    pip install transformers accelerate faster-whisper openwakeword kokoro soundfile sounddevice pyautogui numpy
    # Kokoro needs espeak-ng: https://github.com/espeak-ng/espeak-ng/releases (install the .msi)
    # Optional fallback voice: pip install pyttsx3

RUN:
    python jarvis.py          # voice mode (say "Hey Jarvis")
    python jarvis.py --text   # type commands instead (good for debugging)

VRAM plan for RTX 3050 4GB:
    Qwen2.5-1.5B fp16 on GPU  (~3 GB)
    Whisper + wake word on CPU (int8) so everything fits
"""

import argparse
import ctypes
import datetime
import difflib
import json
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import numpy as np
import pyautogui
import pygetwindow as gw
import sounddevice as sd
import torch
from transformers import pipeline

import hud   # the Tony Stark style interface (hud.py + hud.html)

# ----------------------------- CONFIG ---------------------------------
WAKE_WORD = "hey_jarvis"
WAKE_THRESHOLD = 0.5            # lower = more sensitive, higher = fewer false triggers
WHISPER_MODEL = "small"         # "base" is faster, "small" is more accurate (better for accents)
# The brain. Loaded in 4-bit so it fits in 4 GB of VRAM (needs: pip install -U bitsandbytes).
#   "Qwen/Qwen2.5-3B-Instruct"        ~2.1 GB VRAM  - safe default, big step up from 1.5B
#   "Qwen/Qwen3-4B-Instruct-2507"     ~2.8 GB VRAM  - smarter, but tight on 4 GB (close other GPU apps)
LLM_MODEL = "Qwen/Qwen2.5-3B-Instruct"
FALLBACK_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"   # used automatically if the 4-bit load fails
TTS_VOICE = "bm_george"         # British male. Try "bm_lewis", or "am_adam" for American
SAMPLE_RATE = 16000
CHUNK = 1280                    # 80 ms of audio, what openWakeWord expects
SILENCE_SECONDS = 1.2           # stop recording after this much silence
MAX_RECORD_SECONDS = 10
FOLLOWUP_SECONDS = 8            # after a command, keep listening this long for the next one (no wake word needed)
ALWAYS_LISTEN = True            # True: after "Hey Jarvis" he keeps listening until you say "go to sleep". False: old 8-second timeout
SLEEP_PHRASES = r"\b(go to sleep|stop listening|stand ?by|sleep mode)\b"
INSTAGRAM_USER = ""             # optional: your Instagram username, enables "my profile" and "saved posts"
BROWSER = "chrome"              # searches and websites open here (new tab in your existing window). "msedge" for Edge
# -----------------------------------------------------------------------

pyautogui.FAILSAFE = True       # slam the mouse into a screen corner to abort pyautogui actions

# ---------------------------- LOAD MODELS ------------------------------
print("Loading models (first run downloads them, be patient)...")

def load_llm():
    gpu = torch.cuda.is_available()
    if gpu:
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
            quant = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_use_double_quant=True,
            )
            tok = AutoTokenizer.from_pretrained(LLM_MODEL)
            model = AutoModelForCausalLM.from_pretrained(
                LLM_MODEL, quantization_config=quant, device_map={"": 0}, dtype=torch.float16
            )
            print(f"LLM: {LLM_MODEL} (4-bit, GPU)")
            return pipeline("text-generation", model=model, tokenizer=tok)
        except Exception as e:
            print(f"4-bit load of {LLM_MODEL} failed ({e}).\nFalling back to {FALLBACK_MODEL}.")
            torch.cuda.empty_cache()
    print(f"LLM: {FALLBACK_MODEL} ({'GPU' if gpu else 'CPU'})")
    return pipeline(
        "text-generation",
        model=FALLBACK_MODEL,
        dtype=torch.float16 if gpu else torch.float32,
        device=0 if gpu else -1,
    )


llm = load_llm()

# Text-to-speech: Kokoro, with pyttsx3 as fallback
tts_engine = None
kokoro = None
try:
    from kokoro import KPipeline
    kokoro = KPipeline(lang_code="b")  # "b" = British English, "a" = American
except Exception as e:
    print("Kokoro unavailable, falling back to pyttsx3:", e)
    import pyttsx3
    tts_engine = pyttsx3.init()
    tts_engine.setProperty("rate", 175)


def _pulse(a):
    """Send the voice loudness and frequency-band energy to the HUD in 50 ms steps while Jarvis speaks."""
    for i in range(0, len(a), 1200):
        chunk = a[i:i + 1200]
        if len(chunk) == 0:
            continue
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        level = min(1.0, rms / 0.15)
        # Simple frequency-band analysis from the actual speech audio (24 kHz Kokoro output)
        try:
            fft = np.abs(np.fft.rfft(chunk))
            freqs = np.fft.rfftfreq(len(chunk), 1 / 24000)
            low_mask = (freqs >= 80) & (freqs < 250)
            mid_mask = (freqs >= 250) & (freqs < 2000)
            high_mask = (freqs >= 2000) & (freqs < 8000)
            low = float(np.mean(fft[low_mask])) if np.any(low_mask) else 0.0
            mid = float(np.mean(fft[mid_mask])) if np.any(mid_mask) else 0.0
            high = float(np.mean(fft[high_mask])) if np.any(high_mask) else 0.0
            max_fft = max(float(np.max(fft)), 1e-10)
            bands = [
                min(1.0, low / max_fft * 6.0),
                min(1.0, mid / max_fft * 6.0),
                min(1.0, high / max_fft * 6.0),
            ]
        except Exception:
            bands = [level, level, level]
        hud.emit("level", v=round(level, 3), bands=bands)
        time.sleep(0.05)


def speak(text: str):
    print("Jarvis:", text)
    if not text:
        return
    hud.state("speaking")
    hud.emit("jarvis", text=text)
    try:
        if kokoro is not None:
            for _, _, audio in kokoro(text, voice=TTS_VOICE):
                if hasattr(audio, "cpu"):
                    audio = audio.cpu().numpy()
                a = np.asarray(audio, dtype=np.float32)
                threading.Thread(target=_pulse, args=(a,), daemon=True).start()
                sd.play(a, 24000)
                sd.wait()
        else:
            tts_engine.say(text)
            tts_engine.runAndWait()
    except Exception as e:
        print("(speech failed)", e)
    hud.state("idle")


# ------------------------------ ACTIONS --------------------------------
HOME = Path.home()
FOLDERS = {
    "downloads": HOME / "Downloads",
    "documents": HOME / "Documents",
    "desktop": HOME / "Desktop",
    "pictures": HOME / "Pictures",
    "music": HOME / "Music",
    "videos": HOME / "Videos",
}

APPS = {
    "chrome": "chrome", "browser": "chrome", "google chrome": "chrome",
    "edge": "msedge", "firefox": "firefox",
    "notepad": "notepad", "calculator": "calc", "paint": "mspaint",
    "vs code": "code", "vscode": "code", "code": "code", "visual studio code": "code",
    "spotify": "spotify", "explorer": "explorer", "file explorer": "explorer",
    "terminal": "wt", "command prompt": "cmd", "cmd": "cmd", "powershell": "powershell",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "settings": "ms-settings:", "task manager": "taskmgr",
}


def open_app(a):
    name = a["name"].lower().strip()
    target = APPS.get(name)
    if target is None:
        close = difflib.get_close_matches(name, list(APPS), n=1, cutoff=0.75)
        target = APPS[close[0]] if close else name
    r = subprocess.run(f'start "" "{target}"', shell=True, capture_output=True)
    if r.returncode != 0:
        speak(f"I can't find an app called {name}.")


def open_url(url):
    """Open a URL as a new tab in BROWSER (reuses the open window and brings it to the front)."""
    r = subprocess.run(f'start "" {BROWSER} "{url}"', shell=True, capture_output=True)
    if r.returncode != 0:
        webbrowser.open(url)   # fallback: Windows default browser


def open_website(a):
    url = a["url"].strip()
    if not url.startswith("http"):
        url = "https://" + url
    open_url(url)


def google_search(a):
    open_url("https://www.google.com/search?q=" + urllib.parse.quote(a["query"]))


def youtube_search(a):
    open_url("https://www.youtube.com/results?search_query=" + urllib.parse.quote(a["query"]))


def open_folder(a):
    key = a["path"].lower().strip()
    path = FOLDERS.get(key, Path(a["path"]))
    subprocess.Popen(["explorer", str(path)])


def type_text(a):
    time.sleep(0.5)
    title = _active_title()
    print("Typing into window:", repr(title))   # debug: shows which window gets the keystrokes
    if "powershell" in title or "command prompt" in title or title.startswith("j.a.r.v.i.s"):
        speak("Click the window you want me to type in first.")
        return
    if a.get("search_box") in (True, "true", "True") and "new tab" not in title:
        pyautogui.press("/")        # the "/" key focuses the site's search box on YouTube, GitHub, X and others
        time.sleep(0.3)             # (skipped on a New Tab page, where the address bar is already ready to type)
    pyautogui.write(a["text"], interval=0.02)
    if a.get("enter") in (True, "true", "True"):
        pyautogui.press("enter")


def press_keys(a):
    keys = [k.lower() for k in a["keys"]]
    pyautogui.hotkey(*keys)


def screenshot(a):
    out = FOLDERS["pictures"] / f"jarvis_{int(time.time())}.png"
    pyautogui.screenshot(str(out))


def volume(a):
    d = a.get("direction", "up")
    if d == "mute":
        pyautogui.press("volumemute")
    else:
        for _ in range(int(a.get("steps", 5))):
            pyautogui.press("volumeup" if d == "up" else "volumedown")


def media(a):
    pyautogui.press(a.get("key", "playpause"))  # playpause | nexttrack | prevtrack


def lock_screen(a):
    ctypes.windll.user32.LockWorkStation()


def wait(a):
    time.sleep(min(float(a.get("seconds", 1)), 10))


WINDOW_ALIASES = {
    "chrome": "chrome", "browser": "chrome", "google chrome": "chrome",
    "edge": "edge", "firefox": "firefox",
    "vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
    "notepad": "notepad", "word": "word", "excel": "excel", "powerpoint": "powerpoint",
    "spotify": "spotify", "terminal": "terminal", "settings": "settings",
    "explorer": "file explorer", "file explorer": "file explorer",
}

# Windows Jarvis refuses to close by voice, so he can't close the terminal he runs in.
# Edit this list if you want to allow more (or fewer).
PROTECTED_TITLES = ["powershell", "command prompt", "windows terminal", "visual studio code", "jarvis"]


def find_window(name):
    """Find an open window whose title matches `name` (partial or fuzzy match)."""
    target = WINDOW_ALIASES.get(name.lower().strip(), name.lower().strip())
    wins = [w for w in gw.getAllWindows() if w.title.strip()]
    for w in wins:
        if target in w.title.lower():
            return w
    titles = {w.title.lower(): w for w in wins}
    close = difflib.get_close_matches(target, list(titles), n=1, cutoff=0.5)
    return titles[close[0]] if close else None


def focus(win):
    try:
        if win.isMinimized:
            win.restore()
        win.activate()
    except Exception as e:
        if "Error code from Windows: 0" in str(e):   # harmless pygetwindow quirk, it worked
            return
        pyautogui.press("alt")                       # trick that lets Windows allow the focus change
        try:
            win.activate()
        except Exception as e2:
            if "Error code from Windows: 0" not in str(e2):
                print("Could not focus window:", e2)


def switch_window(a):
    w = find_window(a["name"])
    if w is None:
        speak(f"I can't find a window called {a['name']}.")
        return
    focus(w)


def close_window(a):
    name = a.get("name")
    w = find_window(name) if name else gw.getActiveWindow()
    if w is None or not w.title.strip():
        speak("I don't see a window to close.")
        return
    if any(p in w.title.lower() for p in PROTECTED_TITLES):
        speak("I won't close that one, it's protected.")
        return
    w.close()   # polite close: apps will still ask about unsaved work


def minimize_all(a):
    pyautogui.hotkey("win", "m")


def restore_all(a):
    pyautogui.hotkey("win", "shift", "m")


SCROLL_CLICKS = {"small": 3, "medium": 8, "large": 16}        # number of mouse-wheel notches
SCROLL_DELAY = {"small": 0.25, "medium": 0.12, "large": 0.05}  # seconds between notches when auto-scrolling
WHEEL = 120   # pyautogui scroll units per mouse-wheel notch on Windows
_scrolling = threading.Event()


def _aim_at_active_window():
    """Mouse-wheel scrolling goes to whatever is under the pointer, so move it onto the active window."""
    try:
        w = gw.getActiveWindow()
        if w and w.width > 0 and w.height > 0 and not w.isMinimized:
            mx, my = pyautogui.position()
            if not (w.left <= mx <= w.left + w.width and w.top <= my <= w.top + w.height):
                pyautogui.moveTo(w.left + w.width // 2, w.top + w.height // 2)
    except Exception:
        pass


def _auto_scroll(direction, delay):
    sign = 1 if direction in ("up", "right") else -1
    end = time.time() + 90                 # safety: never scroll for more than 90 seconds
    while _scrolling.is_set() and time.time() < end:
        if direction in ("left", "right"):
            pyautogui.hscroll(sign * WHEEL)
        else:
            pyautogui.scroll(sign * WHEEL)
        time.sleep(delay)
    _scrolling.clear()


def scroll(a):
    d = str(a.get("direction", "down")).lower()
    amount = str(a.get("amount", "medium")).lower()
    clicks = SCROLL_CLICKS.get(amount)
    if clicks is None:
        try:
            clicks = max(1, min(int(amount), 50))
        except ValueError:
            clicks = 8
    _aim_at_active_window()
    if d == "top":
        pyautogui.hotkey("ctrl", "home")
    elif d == "bottom":
        pyautogui.hotkey("ctrl", "end")
    elif a.get("continuous") in (True, "true", "True"):
        _scrolling.set()
        threading.Thread(target=_auto_scroll, args=(d, SCROLL_DELAY.get(amount, 0.12)), daemon=True).start()
    elif d in ("up", "down"):
        pyautogui.scroll((clicks if d == "up" else -clicks) * WHEEL)
    elif d in ("left", "right"):
        pyautogui.hscroll((clicks if d == "right" else -clicks) * WHEEL)


def stop_scroll(a):
    _scrolling.clear()


BROWSER_TAGS = ("google chrome", "microsoft edge", "mozilla firefox", "brave", "opera")


def _browser_window():
    """Return the active browser window, or bring one to the front."""
    w = gw.getActiveWindow()
    if w and any(t in w.title.lower() for t in BROWSER_TAGS):
        return w
    for w in gw.getAllWindows():
        if any(t in w.title.lower() for t in BROWSER_TAGS):
            focus(w)
            time.sleep(0.3)
            return w
    return None


def _active_title():
    w = gw.getActiveWindow()
    return w.title.lower() if w else ""


def _find_tab(name):
    """Step through the tabs until one's title contains `name`. True if found (that tab is then active)."""
    name = name.lower().strip()
    if name in _active_title():
        return True
    start = _active_title()
    for _ in range(40):
        pyautogui.hotkey("ctrl", "tab")
        time.sleep(0.15)
        t = _active_title()
        if name in t:
            return True
        if t == start:          # went all the way round
            break
    return False


def _tab_number(a):
    try:
        return int(a.get("number"))
    except (TypeError, ValueError):
        return None


def close_tab(a):
    if not _browser_window():
        speak("I don't see a browser window.")
        return
    n = _tab_number(a)
    if n:
        if not 1 <= n <= 8:
            speak("I can only reach tabs one to eight by number.")
            return
        pyautogui.hotkey("ctrl", str(n))
        time.sleep(0.15)
    elif a.get("name"):
        if not _find_tab(str(a["name"])):
            speak(f"I can't find a tab called {a['name']}.")
            return
    pyautogui.hotkey("ctrl", "w")        # closes just that tab, not the whole browser


def switch_tab(a):
    if not _browser_window():
        speak("I don't see a browser window.")
        return
    d = str(a.get("direction", "")).lower()
    n = _tab_number(a)
    if d == "previous":
        pyautogui.hotkey("ctrl", "shift", "tab")
    elif d == "next":
        pyautogui.hotkey("ctrl", "tab")
    elif n:
        pyautogui.hotkey("ctrl", str(min(n, 9)))   # ctrl+9 jumps to the last tab
    elif a.get("name"):
        if not _find_tab(str(a["name"])):
            speak(f"I can't find a tab called {a['name']}.")


def reopen_tab(a):
    if _browser_window():
        pyautogui.hotkey("ctrl", "shift", "t")


def new_tab(a):
    if not _browser_window():
        open_url("https://www.google.com")   # no browser open yet: start one
        return
    pyautogui.hotkey("ctrl", "t")


INSTAGRAM_PAGES = {
    "home": "https://www.instagram.com/",
    "messages": "https://www.instagram.com/direct/inbox/",
    "dms": "https://www.instagram.com/direct/inbox/",
    "inbox": "https://www.instagram.com/direct/inbox/",
    "explore": "https://www.instagram.com/explore/",
    "reels": "https://www.instagram.com/reels/",
    "notifications": "https://www.instagram.com/accounts/activity/",
}


def instagram(a):
    section = str(a.get("section", "home")).lower().strip()
    user = str(a.get("user", "")).strip().lstrip("@").replace(" ", "")
    if user:                                   # someone's profile
        open_url("https://www.instagram.com/" + urllib.parse.quote(user) + "/")
        return
    if section in ("profile", "saved"):        # your own pages need your username
        if not INSTAGRAM_USER:
            speak("Set your Instagram username in the config first.")
            return
        open_url(f"https://www.instagram.com/{INSTAGRAM_USER}/" + ("saved/" if section == "saved" else ""))
        return
    url = INSTAGRAM_PAGES.get(section)
    if url is None:
        speak("I don't know that Instagram section.")
        return
    open_url(url)


def reel(a):
    """Next / previous reel (arrow keys work on the Instagram Reels page)."""
    if not _browser_window():
        speak("I don't see a browser window.")
        return
    pyautogui.press("up" if str(a.get("direction", "next")).lower() == "previous" else "down")


# ----------------------------- SYSTEM ACTIONS -----------------------------


def system_power(a):
    """Shutdown, restart, sleep, hibernate, or cancel a pending shutdown."""
    mode = str(a.get("mode", "shutdown")).lower().strip()
    if mode in ("shutdown", "shut down"):
        subprocess.run("shutdown /s /t 60", shell=True, capture_output=True)
        speak("Shutting down in 60 seconds. Say cancel shutdown to abort.")
    elif mode in ("restart", "reboot"):
        subprocess.run("shutdown /r /t 60", shell=True, capture_output=True)
        speak("Restarting in 60 seconds.")
    elif mode == "sleep":
        subprocess.run("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True, capture_output=True)
        speak("Going to sleep.")
    elif mode == "hibernate":
        subprocess.run("shutdown /h", shell=True, capture_output=True)
        speak("Hibernating.")
    elif mode in ("cancel", "abort"):
        subprocess.run("shutdown /a", shell=True, capture_output=True)
        speak("Shutdown cancelled.")
    else:
        speak("I didn't understand that power option.")


def empty_recycle_bin(a):
    """Empty the Windows Recycle Bin."""
    r = subprocess.run(
        'powershell -Command "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"',
        shell=True,
        capture_output=True,
    )
    if r.returncode == 0:
        speak("Recycle bin emptied.")
    else:
        speak("I couldn't empty the recycle bin.")


def system_info(a):
    """Speak basic system stats: CPU, RAM, disk, battery."""
    try:
        import psutil

        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage("C:\\").percent
        parts = [
            f"CPU at {cpu} percent",
            f"memory at {mem} percent",
            f"C drive at {disk} percent",
        ]
        b = psutil.sensors_battery()
        if b:
            parts.append(
                f"battery at {int(b.percent)} percent "
                f"{'plugged in' if b.power_plugged else 'on battery'}"
            )
        speak(", ".join(parts) + ".")
    except Exception:
        speak("I can't read system stats right now.")


def ip_address(a):
    """Speak the local IP address."""
    try:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        speak(f"Your local IP address is {ip}.")
    except Exception:
        speak("I couldn't find your IP address.")


# ----------------------------- WINDOW ACTIONS -----------------------------


def window_maximize(a):
    """Maximize the active window."""
    w = gw.getActiveWindow()
    if w:
        try:
            w.maximize()
            speak("Maximized.")
        except Exception:
            speak("Couldn't maximize that window.")
    else:
        speak("No active window to maximize.")


def window_restore(a):
    """Restore (un-maximize) the active window."""
    w = gw.getActiveWindow()
    if w:
        try:
            w.restore()
            speak("Window restored.")
        except Exception:
            speak("Couldn't restore that window.")
    else:
        speak("No active window to restore.")


def window_snap(a):
    """Snap active window left or right."""
    side = str(a.get("side", "left")).lower()
    w = gw.getActiveWindow()
    if not w:
        speak("No active window.")
        return
    if side == "left":
        pyautogui.hotkey("win", "left")
        speak("Snapped left.")
    elif side == "right":
        pyautogui.hotkey("win", "right")
        speak("Snapped right.")
    else:
        speak("Say snap left or snap right.")


def show_desktop(a):
    """Minimize everything to show the desktop."""
    pyautogui.hotkey("win", "d")
    speak("Desktop showing.")


def toggle_fullscreen(a):
    """Toggle fullscreen in the active window (usually F11)."""
    pyautogui.press("f11")
    speak("Toggled fullscreen.")


def task_view(a):
    """Open Windows Task View."""
    pyautogui.hotkey("win", "tab")
    speak("Task view.")


def action_center(a):
    """Open Windows Action Center / notifications."""
    pyautogui.hotkey("win", "a")
    speak("Action center.")


# ----------------------------- SETTINGS & TOOLS -----------------------------


def open_settings_page(a):
    """Open a specific Windows Settings page."""
    page = str(a.get("page", "")).lower().strip()
    pages = {
        "network": "ms-settings:network",
        "wifi": "ms-settings:network-wifi",
        "sound": "ms-settings:sound",
        "display": "ms-settings:display",
        "apps": "ms-settings:appsfeatures",
        "privacy": "ms-settings:privacy",
        "accounts": "ms-settings:accounts",
        "time": "ms-settings:time-language",
        "update": "ms-settings:windowsupdate",
        "bluetooth": "ms-settings:bluetooth",
        "personalization": "ms-settings:personalization",
    }
    target = pages.get(page)
    if not target:
        close = difflib.get_close_matches(page, list(pages), n=1, cutoff=0.6)
        target = pages[close[0]] if close else None
    if target:
        subprocess.run(f'start "" "{target}"', shell=True, capture_output=True)
        speak(f"Opening {page} settings.")
    else:
        speak("I don't know that settings page.")


def open_control_panel(a):
    """Open Control Panel."""
    subprocess.run("control", shell=True)
    speak("Opening Control Panel.")


def open_device_manager(a):
    """Open Device Manager."""
    subprocess.run("devmgmt.msc", shell=True)
    speak("Opening Device Manager.")


def open_task_manager(a):
    """Open Task Manager."""
    subprocess.run("taskmgr", shell=True)
    speak("Opening Task Manager.")


def open_command_prompt_admin(a):
    """Open Command Prompt as administrator."""
    subprocess.run('powershell -Command "Start-Process cmd -Verb RunAs"', shell=True)
    speak("Opening Command Prompt as administrator.")


# ----------------------------- CLIPBOARD & TEXT ACTIONS -----------------------------


def clipboard_copy(a):
    pyautogui.hotkey("ctrl", "c")
    speak("Copied.")


def clipboard_paste(a):
    pyautogui.hotkey("ctrl", "v")
    speak("Pasted.")


def clipboard_cut(a):
    pyautogui.hotkey("ctrl", "x")
    speak("Cut.")


def select_all(a):
    pyautogui.hotkey("ctrl", "a")
    speak("Selected all.")


def undo(a):
    pyautogui.hotkey("ctrl", "z")
    speak("Undone.")


def redo(a):
    pyautogui.hotkey("ctrl", "y")
    speak("Redone.")


# ----------------------------- BROWSER ACTIONS -----------------------------


def browser_action(a):
    """Refresh, back, forward, zoom, bookmark, history, downloads, incognito."""
    cmd = str(a.get("cmd", "")).lower().strip()
    if cmd == "refresh":
        pyautogui.hotkey("ctrl", "r")
        speak("Refreshed.")
    elif cmd == "back":
        pyautogui.hotkey("alt", "left")
        speak("Going back.")
    elif cmd == "forward":
        pyautogui.hotkey("alt", "right")
        speak("Going forward.")
    elif cmd == "zoom in":
        pyautogui.hotkey("ctrl", "=")
        speak("Zoomed in.")
    elif cmd == "zoom out":
        pyautogui.hotkey("ctrl", "-")
        speak("Zoomed out.")
    elif cmd == "zoom reset":
        pyautogui.hotkey("ctrl", "0")
        speak("Zoom reset.")
    elif cmd == "bookmark":
        pyautogui.hotkey("ctrl", "d")
        speak("Bookmarked.")
    elif cmd == "history":
        pyautogui.hotkey("ctrl", "h")
        speak("Opening history.")
    elif cmd == "downloads":
        pyautogui.hotkey("ctrl", "j")
        speak("Opening downloads.")
    elif cmd == "incognito":
        pyautogui.hotkey("ctrl", "shift", "n")
        speak("Opening incognito window.")
    else:
        speak("I don't know that browser command.")


def browser_type(a):
    """Type text into Chrome address bar (Google search) or YouTube search bar."""
    target = str(a.get("target", "chrome")).lower().strip()
    text = str(a.get("text", "")).strip()
    if not text:
        speak("What should I type?")
        return
    if target in ("youtube", "yt"):
        open_url("https://www.youtube.com/results?search_query=" + urllib.parse.quote(text))
        speak(f"Searching YouTube for {text}.")
        return
    # Chrome / Google address bar (omnibox)
    w = _browser_window()
    if w is None:
        open_url("https://www.google.com")
        time.sleep(1.5)
    else:
        focus(w)
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "t")   # fresh tab
        time.sleep(0.4)
    pyautogui.hotkey("ctrl", "l")       # focus address bar
    time.sleep(0.4)
    pyautogui.write(text, interval=0.02)
    pyautogui.press("enter")
    speak(f"Typed {text} in Chrome.")


ACTIONS = {
    "open_app": open_app,
    "open_website": open_website,
    "google_search": google_search,
    "youtube_search": youtube_search,
    "open_folder": open_folder,
    "type_text": type_text,
    "press_keys": press_keys,
    "screenshot": screenshot,
    "volume": volume,
    "media": media,
    "lock_screen": lock_screen,
    "wait": wait,
    "scroll": scroll,
    "stop_scroll": stop_scroll,
    "close_tab": close_tab,
    "switch_tab": switch_tab,
    "reopen_tab": reopen_tab,
    "new_tab": new_tab,
    "instagram": instagram,
    "reel": reel,
    "switch_window": switch_window,
    "close_window": close_window,
    "minimize_all": minimize_all,
    "restore_all": restore_all,
    # ---------------- system / power ----------------
    "system_power": system_power,
    "empty_recycle_bin": empty_recycle_bin,
    "system_info": system_info,
    "ip_address": ip_address,
    # ---------------- window management ----------------
    "window_maximize": window_maximize,
    "window_restore": window_restore,
    "window_snap": window_snap,
    "show_desktop": show_desktop,
    "toggle_fullscreen": toggle_fullscreen,
    "task_view": task_view,
    "action_center": action_center,
    # ---------------- settings & tools ----------------
    "open_settings_page": open_settings_page,
    "open_control_panel": open_control_panel,
    "open_device_manager": open_device_manager,
    "open_task_manager": open_task_manager,
    "open_command_prompt_admin": open_command_prompt_admin,
    # ---------------- clipboard & text ----------------
    "clipboard_copy": clipboard_copy,
    "clipboard_paste": clipboard_paste,
    "clipboard_cut": clipboard_cut,
    "select_all": select_all,
    "undo": undo,
    "redo": redo,
    # ---------------- browser ----------------
    "browser_action": browser_action,
    "browser_type": browser_type,
}

# ------------------------------- BRAIN ---------------------------------
SYSTEM = """You are Jarvis, a witty, concise voice assistant that controls a Windows laptop.
Reply with ONLY one JSON object and nothing else, in this format:
{"say": "<very short spoken reply, max 12 words>", "actions": [<zero or more actions, run in order>]}

Available actions:
{"action": "open_app", "name": "<app name>"}
{"action": "open_website", "url": "<url>"}
{"action": "google_search", "query": "<text>"}
{"action": "youtube_search", "query": "<text>"}
{"action": "open_folder", "path": "<downloads|documents|desktop|pictures|music|videos|full path>"}
{"action": "type_text", "text": "<text>", "search_box": <optional true: focus the website's search box first>, "enter": <optional true: press Enter after typing>}
{"action": "press_keys", "keys": ["ctrl", "t"]}
{"action": "screenshot"}
{"action": "volume", "direction": "up|down|mute"}
{"action": "media", "key": "playpause|nexttrack|prevtrack"}
{"action": "lock_screen"}
{"action": "wait", "seconds": 2}
{"action": "switch_window", "name": "<part of the window title, e.g. chrome, notepad, visual studio code>"}
{"action": "close_window", "name": "<optional app/window name; leave out to close the current window>"}
{"action": "minimize_all"}
{"action": "restore_all"}
{"action": "scroll", "direction": "up|down|left|right|top|bottom", "amount": "small|medium|large", "continuous": false}
{"action": "stop_scroll"}
{"action": "close_tab", "name": "<optional: part of the tab title>", "number": <optional tab number 1-8>}
{"action": "switch_tab", "name": "<optional: part of the tab title>", "number": <optional 1-9>, "direction": "next|previous"}
{"action": "reopen_tab"}
{"action": "new_tab"}
{"action": "instagram", "section": "home|messages|explore|reels|notifications|profile|saved", "user": "<optional: open this person's profile>"}
{"action": "reel", "direction": "next|previous"}
{"action": "system_power", "mode": "shutdown|restart|sleep|hibernate|cancel"}
{"action": "empty_recycle_bin"}
{"action": "system_info"}
{"action": "ip_address"}
{"action": "window_maximize"}
{"action": "window_restore"}
{"action": "window_snap", "side": "left|right"}
{"action": "show_desktop"}
{"action": "toggle_fullscreen"}
{"action": "task_view"}
{"action": "action_center"}
{"action": "open_settings_page", "page": "network|wifi|sound|display|apps|privacy|accounts|time|update|bluetooth|personalization"}
{"action": "open_control_panel"}
{"action": "open_device_manager"}
{"action": "open_task_manager"}
{"action": "open_command_prompt_admin"}
{"action": "clipboard_copy"}
{"action": "clipboard_paste"}
{"action": "clipboard_cut"}
{"action": "select_all"}
{"action": "undo"}
{"action": "redo"}
{"action": "browser_action", "cmd": "refresh|back|forward|zoom in|zoom out|zoom reset|bookmark|history|downloads|incognito"}
{"action": "browser_type", "target": "chrome|youtube|yt", "text": "<text to type>"}
{"action": "exit"}

Use close_tab/switch_tab/new_tab when the user says "tab" (browser tabs). Use close_window only for closing a whole app or window.
To search or open a website, always use google_search, youtube_search or open_website (they work even if the browser is already open). Never use press_keys for that.

If no action fits (a question or chat), use an empty actions list and put the answer in "say".

Examples:
User: open chrome and search for biryani in pune
{"say": "Fetching biryani, sir.", "actions": [{"action": "google_search", "query": "biryani in pune"}]}
User: play lofi on youtube
{"say": "Lofi incoming.", "actions": [{"action": "youtube_search", "query": "lofi"}]}
User: open notepad and type hello world
{"say": "Opening notepad.", "actions": [{"action": "open_app", "name": "notepad"}, {"action": "wait", "seconds": 2}, {"action": "type_text", "text": "hello world"}]}
User: turn the volume down
{"say": "Quieter now.", "actions": [{"action": "volume", "direction": "down"}]}
User: type hello
{"say": "Typing.", "actions": [{"action": "type_text", "text": "hello"}]}
User: type lofi in the search box and press enter
{"say": "Searching.", "actions": [{"action": "type_text", "text": "lofi", "search_box": true, "enter": true}]}
User: switch to chrome
{"say": "Switching to Chrome.", "actions": [{"action": "switch_window", "name": "chrome"}]}
User: close this window
{"say": "Closing it.", "actions": [{"action": "close_window"}]}
User: close notepad
{"say": "Closing Notepad.", "actions": [{"action": "close_window", "name": "notepad"}]}
User: minimize everything
{"say": "Clearing the screen.", "actions": [{"action": "minimize_all"}]}
User: bring everything back
{"say": "Restoring your windows.", "actions": [{"action": "restore_all"}]}
User: scroll down
{"say": "Scrolling down.", "actions": [{"action": "scroll", "direction": "down", "amount": "medium"}]}
User: scroll up a little
{"say": "Up a bit.", "actions": [{"action": "scroll", "direction": "up", "amount": "small"}]}
User: go to the top of the page
{"say": "Taking you to the top.", "actions": [{"action": "scroll", "direction": "top"}]}
User: keep scrolling down
{"say": "Scrolling. Say stop when ready.", "actions": [{"action": "scroll", "direction": "down", "amount": "medium", "continuous": true}]}
User: stop scrolling
{"say": "Stopped.", "actions": [{"action": "stop_scroll"}]}
User: close this tab
{"say": "Closing the tab.", "actions": [{"action": "close_tab"}]}
User: close the youtube tab
{"say": "Closing the YouTube tab.", "actions": [{"action": "close_tab", "name": "youtube"}]}
User: close the third tab
{"say": "Closing tab three.", "actions": [{"action": "close_tab", "number": 3}]}
User: switch to the gmail tab
{"say": "Going to Gmail.", "actions": [{"action": "switch_tab", "name": "gmail"}]}
User: next tab
{"say": "Next tab.", "actions": [{"action": "switch_tab", "direction": "next"}]}
User: reopen the tab I closed
{"say": "Bringing it back.", "actions": [{"action": "reopen_tab"}]}
User: open a new tab
{"say": "New tab.", "actions": [{"action": "new_tab"}]}
User: open instagram
{"say": "Opening Instagram.", "actions": [{"action": "instagram", "section": "home"}]}
User: open my instagram messages
{"say": "Opening your DMs.", "actions": [{"action": "instagram", "section": "messages"}]}
User: show instagram reels
{"say": "Reels, sir.", "actions": [{"action": "instagram", "section": "reels"}]}
User: next reel
{"say": "Next one.", "actions": [{"action": "reel", "direction": "next"}]}
User: search for cricket scores
{"say": "Searching.", "actions": [{"action": "google_search", "query": "cricket scores"}]}
User: who are you
{"say": "I'm Jarvis, your laptop's new best friend.", "actions": []}
User: shut down the computer
{"say": "Shutting down in one minute.", "actions": [{"action": "system_power", "mode": "shutdown"}]}
User: restart now
{"say": "Restarting shortly.", "actions": [{"action": "system_power", "mode": "restart"}]}
User: put the pc to sleep
{"say": "Going to sleep.", "actions": [{"action": "system_power", "mode": "sleep"}]}
User: cancel the shutdown
{"say": "Shutdown cancelled.", "actions": [{"action": "system_power", "mode": "cancel"}]}
User: empty the recycle bin
{"say": "Emptying the recycle bin.", "actions": [{"action": "empty_recycle_bin"}]}
User: how is the system doing
{"say": "Checking your stats.", "actions": [{"action": "system_info"}]}
User: what is my ip address
{"say": "Looking it up.", "actions": [{"action": "ip_address"}]}
User: maximize this window
{"say": "Maximizing.", "actions": [{"action": "window_maximize"}]}
User: snap this window to the left
{"say": "Snapping left.", "actions": [{"action": "window_snap", "side": "left"}]}
User: show the desktop
{"say": "Showing desktop.", "actions": [{"action": "show_desktop"}]}
User: toggle fullscreen
{"say": "Toggling fullscreen.", "actions": [{"action": "toggle_fullscreen"}]}
User: open task manager
{"say": "Opening Task Manager.", "actions": [{"action": "open_task_manager"}]}
User: open device manager
{"say": "Opening Device Manager.", "actions": [{"action": "open_device_manager"}]}
User: open control panel
{"say": "Opening Control Panel.", "actions": [{"action": "open_control_panel"}]}
User: open wifi settings
{"say": "Opening Wi-Fi settings.", "actions": [{"action": "open_settings_page", "page": "wifi"}]}
User: open network settings
{"say": "Opening network settings.", "actions": [{"action": "open_settings_page", "page": "network"}]}
User: copy that
{"say": "Copied.", "actions": [{"action": "clipboard_copy"}]}
user: paste
{"say": "Pasting.", "actions": [{"action": "clipboard_paste"}]}
User: select all
{"say": "Selecting all.", "actions": [{"action": "select_all"}]}
User: undo
{"say": "Undoing.", "actions": [{"action": "undo"}]}
User: refresh the page
{"say": "Refreshing.", "actions": [{"action": "browser_action", "cmd": "refresh"}]}
User: go back
{"say": "Going back.", "actions": [{"action": "browser_action", "cmd": "back"}]}
User: zoom in
{"say": "Zooming in.", "actions": [{"action": "browser_action", "cmd": "zoom in"}]}
User: bookmark this page
{"say": "Bookmarking.", "actions": [{"action": "browser_action", "cmd": "bookmark"}]}
User: open task view
{"say": "Opening Task View.", "actions": [{"action": "task_view"}]}
User: open action center
{"say": "Opening Action Center.", "actions": [{"action": "action_center"}]}
User: type python tutorial in chrome
{"say": "Typing in Chrome.", "actions": [{"action": "browser_type", "target": "chrome", "text": "python tutorial"}]}
User: search lofi beats on youtube
{"say": "Searching YouTube.", "actions": [{"action": "browser_type", "target": "youtube", "text": "lofi beats"}]}
User: type weather today in chrome search bar
{"say": "Typing in Chrome.", "actions": [{"action": "browser_type", "target": "chrome", "text": "weather today"}]}
"""


def think(user_text: str) -> dict:
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_text},
    ]
    t0 = time.time()
    out = llm(msgs, max_new_tokens=200, do_sample=False)[0]["generated_text"][-1]["content"]
    print(f"(thought for {time.time() - t0:.1f}s)")
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if not m:
        return {"say": out.strip()[:200], "actions": []}
    return json.loads(m.group(0))


def quick_answer(text: str):
    """Answer time/date/day questions directly (the small LLM can't know them)."""
    t = text.lower()
    now = datetime.datetime.now()
    asks_time = re.search(r"\btime\b", t) and not re.search(r"\b(timer|sometime|sometimes)\b", t)
    asks_date = re.search(r"\b(date|day|today|todays|today's)\b", t)
    if not (asks_time or asks_date):
        return None
    if re.search(r"\b(open|search|play|google|youtube|type|set|remind)\b", t):
        return None
    if not (re.search(r"\b(what|what's|whats|tell|current|which|say)\b", t) or len(t.split()) <= 3):
        return None
    clock = now.strftime("%I:%M %p").lstrip("0")
    day = f"{now.strftime('%A')}, {now.day} {now.strftime('%B %Y')}"
    if asks_time and asks_date:
        return f"It's {clock} on {day}."
    if asks_time:
        return f"It's {clock}."
    return f"Today is {day}."


def run_command(text: str) -> bool:
    """Returns False if Jarvis should shut down."""
    print("You:", text)
    _scrolling.clear()
    hud.emit("user", text=text)
    quick = quick_answer(text)
    if quick:
        speak(quick)
        return True
    hud.state("thinking")
    try:
        plan = think(text)
    except Exception as e:
        print("Parse error:", e)
        hud.state("error")
        speak("Sorry, I didn't catch that properly.")
        return True

    speak(plan.get("say", "Done."))

    for act in plan.get("actions", []):
        name = act.get("action")
        if name == "exit":
            return False
        fn = ACTIONS.get(name)
        if fn is None:
            print("Unknown action:", name)
            continue
        try:
            print("->", act)
            hud.emit("action", name=name, arg=str(next((v for k, v in act.items() if k != "action"), "")))
            fn(act)
        except Exception as e:
            print("Action failed:", name, e)
            speak("That one failed.")
    return True


# ------------------------- EARS (voice mode) ---------------------------
def load_ears():
    from faster_whisper import WhisperModel
    import openwakeword
    from openwakeword.model import Model

    openwakeword.utils.download_models()
    oww = Model(wakeword_models=[WAKE_WORD], inference_framework="onnx")
    stt = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return oww, stt


def rms(chunk):
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))


def calibrate(stream, seconds=1.0):
    levels = []
    for _ in range(int(seconds * SAMPLE_RATE / CHUNK)):
        data, _ = stream.read(CHUNK)
        levels.append(rms(data[:, 0]))
    return max(300.0, float(np.mean(levels)) * 2.5)


def record_command(stream, threshold, wait=4):
    frames, silent_chunks, started = [], 0, False
    t0 = time.time()
    while time.time() - t0 < MAX_RECORD_SECONDS:
        data, _ = stream.read(CHUNK)
        chunk = data[:, 0]
        frames.append(chunk)
        hud.level(min(1.0, rms(chunk) / 4000))
        if rms(chunk) > threshold:
            started, silent_chunks = True, 0
        elif started:
            silent_chunks += 1
            if silent_chunks * CHUNK / SAMPLE_RATE > SILENCE_SECONDS:
                break
        elif time.time() - t0 > wait:   # never started talking
            return None
    if not started:
        return None
    return np.concatenate(frames).astype(np.float32) / 32768.0


STT_HINT = "Jarvis commands: open Chrome, new tab, close tab, switch tab, scroll down, search Google, YouTube, Spotify, Notepad, volume up, minimize, screenshot."


def transcribe(stt, audio):
    peak = float(np.max(np.abs(audio)))
    if peak < 0.01:
        return ""                                  # basically silence, don't let Whisper invent words
    audio = audio * min(0.9 / peak, 10.0)          # boost a quiet mic so Whisper hears it clearly
    segments, _ = stt.transcribe(
        audio,
        language="en",
        beam_size=5,                               # more careful decoding (was 1)
        vad_filter=True,
        initial_prompt=STT_HINT,                   # tells Whisper which words to expect
        condition_on_previous_text=False,
    )
    # drop segments Whisper itself thinks are not speech (this removes made-up phrases like "Search Service")
    return " ".join(s.text for s in segments if s.no_speech_prob < 0.6).strip()


def listen_once(stream, stt, threshold, wait):
    """Record one command and return its text, or None if nothing was heard."""
    hud.state("listening")
    audio = record_command(stream, threshold, wait)
    if audio is None:
        return None
    text = transcribe(stt, audio)
    print("Heard:", repr(text))
    return text if len(text) >= 2 else None


def voice_loop():
    oww, stt = load_ears()
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK)
    stream.start()
    threshold = calibrate(stream)
    speak("Jarvis online.")
    print(f'Listening for "Hey Jarvis"...  (mic threshold {threshold:.0f})')

    running = True
    while running:
        data, _ = stream.read(CHUNK)
        hud.level(min(1.0, rms(data[:, 0]) / 6000))
        scores = oww.predict(data[:, 0])
        top = max(scores.values())
        if top > 0.15:
            print(f"(wake score {top:.2f})")   # debug: shows whether Jarvis hears "Hey Jarvis"
        if top < WAKE_THRESHOLD:
            continue

        print("Wake word heard")
        hud.state("listening")
        # Pause the mic while Jarvis talks so it doesn't hear itself.
        stream.stop()
        speak("Yes?")
        stream.start()

        # After the wake word, keep taking commands until there is a quiet gap,
        # so you can say "open chrome", then "search for X" without saying "Hey Jarvis" again.
        wait = FOLLOWUP_SECONDS if ALWAYS_LISTEN else 4
        if ALWAYS_LISTEN:
            print("(awake: just talk. Say 'go to sleep' to go back to standby)")
        while running:
            text = listen_once(stream, stt, threshold, wait)
            if not text:
                if ALWAYS_LISTEN:
                    continue            # stay awake and keep listening
                if wait == 4:   # nothing heard right after the wake word
                    stream.stop()
                    speak("I didn't catch that.")
                    stream.start()
                break
            if ALWAYS_LISTEN and re.search(SLEEP_PHRASES, text.lower()):
                stream.stop()
                speak("Going to standby. Say Hey Jarvis when you need me.")
                stream.start()
                break
            stream.stop()
            running = run_command(text)
            stream.start()
            wait = FOLLOWUP_SECONDS

        oww.reset()
        if running:
            print('Listening for "Hey Jarvis"...')

    speak("Goodbye.")
    stream.stop()


def text_loop():
    speak("Jarvis online, text mode.")
    while True:
        cmd = input("You: ").strip()
        if not cmd:
            continue
        if cmd.lower() in ("exit", "quit"):
            break
        if not run_command(cmd):
            break
    speak("Goodbye.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", action="store_true", help="type commands instead of using the mic")
    parser.add_argument("--no-hud", action="store_true", help="don't open the HUD window")
    args = parser.parse_args()
    if not args.no_hud:
        hud.start()
    try:
        text_loop() if args.text else voice_loop()
    except KeyboardInterrupt:
        print("\nBye.")
        sys.exit(0)
