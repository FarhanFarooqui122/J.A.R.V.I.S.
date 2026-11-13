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

## Safety
- **Fixed action whitelist** — the LLM can only pick from the actions in `ACTIONS`; there is no action that runs an arbitrary shell command.
- **Protected windows** — `close_window` refuses any window matching `PROTECTED_TITLES` (PowerShell, Command Prompt, Windows Terminal, VS Code, Jarvis itself).
- **Elevated shell is explicit** — `open_command_prompt_admin` starts an admin Command Prompt through the normal Windows UAC prompt, so it is always visible to you.
- **Stays on the machine** — wake word, speech-to-text, the LLM and text-to-speech all run locally; no audio or transcript is sent to a cloud API.

## Adding a new ability
1. Write a Python function that takes the action dict.
2. Add it to the `ACTIONS` dictionary.
3. Add its line and an example to the `SYSTEM` prompt.

## Settings
Everything configurable is at the top of `jarvis.py`:

| Setting | Default | What it does |
| --- | --- | --- |
| `WAKE_WORD` | `hey_jarvis` | openWakeWord model that triggers listening |
| `WAKE_THRESHOLD` | `0.5` | Wake-word sensitivity — lower is more sensitive |
| `WHISPER_MODEL` | `small` | Speech-to-text size; `base` is faster, `small` handles accents better |
| `LLM_MODEL` | `Qwen/Qwen2.5-3B-Instruct` | The brain, loaded in 4-bit |
| `FALLBACK_MODEL` | `Qwen/Qwen2.5-1.5B-Instruct` | Used automatically if the 4-bit load fails |
| `TTS_VOICE` | `bm_george` | Kokoro voice (British male; `bm_lewis`, or `am_adam` for American) |
| `BROWSER` | `chrome` | Where searches and sites open (`msedge` for Edge) |
| `ALWAYS_LISTEN` | `True` | Keep listening after the wake word until "go to sleep" |
| `FOLLOWUP_SECONDS` | `8` | How long it keeps listening for a follow-up command |
| `INSTAGRAM_USER` | `""` | Optional username to enable "my profile" / "saved posts" |

Recording limits live next to them: `SILENCE_SECONDS = 1.2` (stop after this much silence) and
`MAX_RECORD_SECONDS = 10`.

## Troubleshooting
| Symptom | Fix |
| --- | --- |
| Wake word never fires, or fires too often | Tune `WAKE_THRESHOLD` in `jarvis.py` (default `0.5` — lower is more sensitive, higher gives fewer false triggers) |
| "4-bit load ... failed" on startup | `bitsandbytes` is missing or the GPU can't fit the model — `pip install -U bitsandbytes`. Otherwise it continues on the smaller fallback model (`Qwen2.5-1.5B-Instruct`) automatically |
| No speech output / `espeak-ng` error | Kokoro needs [espeak-ng](https://github.com/espeak-ng/espeak-ng/releases) installed (Windows `.msi`); `pyttsx3` is the optional fallback voice |
| Microphone hears nothing | Allow microphone access for the terminal in Windows privacy settings and make sure the right input device is the system default. Run `python jarvis.py --text` to verify the rest of the pipeline without the mic |
| It keeps responding after a command | By design: it listens for `FOLLOWUP_SECONDS` (8 s) without the wake word — say "go to sleep" to stop |
