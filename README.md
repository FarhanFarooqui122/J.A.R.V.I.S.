# J.A.R.V.I.S - local voice assistant for Windows

A fully local, offline Jarvis-style assistant that controls your Windows laptop by voice.

**Pipeline:** microphone -> wake word ("Hey Jarvis", openWakeWord) -> speech-to-text (faster-whisper) -> LLM (Qwen2.5-3B, 4-bit) -> JSON actions -> Python executes them -> text-to-speech (Kokoro).

The LLM replies with JSON only, e.g. `{"say": "Opening Chrome.", "actions": [{"action": "open_app", "name": "chrome"}]}`.
Python runs each action from a fixed whitelist (`ACTIONS` in `jarvis.py`). There is deliberately **no "run any shell command" action**.

## Features
- Wake word, then keeps listening until you say "go to sleep"
- Open apps, websites and folders; Google and YouTube search
- Window control: switch, close (with a protected list), minimize and restore all
- Scrolling (including continuous auto-scroll), browser tab control, new tab
- Instagram sections (messages, explore, reels, profiles) and next/previous reel
- Typing, keyboard shortcuts, screenshots, volume and media keys, lock screen
- Time and date answered directly from the system clock
- Tony-Stark-style HUD in the browser (`hud.py` + `hud.html`)

## Requirements
- Windows, Python 3.12
- NVIDIA GPU with 4 GB VRAM or more recommended (falls back to a smaller model if the 4-bit load fails)
- [espeak-ng](https://github.com/espeak-ng/espeak-ng/releases) (needed by Kokoro)

## Setup
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install torch --index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt
```

## Run
```powershell
python jarvis.py            # voice mode: say "Hey Jarvis"
python jarvis.py --text     # type commands instead
python jarvis.py --no-hud   # without the HUD window
```

## Project layout
| File | Purpose |
| --- | --- |
| `jarvis.py` | Entry point: wake word, STT, LLM prompt, action dispatch, TTS, settings |
| `hud.py` | Serves the HUD page on localhost and streams live events to it |
| `hud.html` | The HUD front-end (no build step, opened by `hud.py`) |
| `requirements.txt` | Python dependencies |
| `.gitignore` | Keeps `.venv/`, caches, logs and backups out of git |

## Adding a new ability
1. Write a Python function that takes the action dict.
2. Add it to the `ACTIONS` dictionary.
3. Add its line and an example to the `SYSTEM` prompt.

## Settings
Everything configurable is at the top of `jarvis.py`: models, wake-word sensitivity, voice, browser, `ALWAYS_LISTEN`, `INSTAGRAM_USER`.
