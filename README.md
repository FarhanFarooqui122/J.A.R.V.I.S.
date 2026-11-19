# J.A.R.V.I.S

A fully local, offline Jarvis-style voice assistant for Windows. Talk to your laptop and it opens apps, searches the web, controls windows, types for you, and answers questions — with a Tony Stark-style HUD.

Everything runs on your machine: wake word, speech-to-text, the language model and text-to-speech. No audio or transcript ever leaves the PC.

```
microphone
   │
   ▼
openWakeWord ("Hey Jarvis")
   │
   ▼
faster-whisper (speech-to-text)
   │
   ▼
Qwen2.5-3B-Instruct, 4-bit (JSON only)
   │
   ▼
Python action dispatcher (fixed whitelist)
   │
   ├──▶ Windows / browser actions
   └──▶ Kokoro (text-to-speech)
```

The model replies with JSON only, for example:

```json
{"say": "Opening Chrome.", "actions": [{"action": "open_app", "name": "chrome"}]}
```

Python executes each action from the whitelist in `ACTIONS` (`jarvis.py`). There is deliberately **no "run any shell command" action**.

## Features

**Voice loop**
- Wake word "Hey Jarvis", then keeps listening until you say "go to sleep"
- Optional 8-second follow-up window after each command
- Works without a microphone via `--text`

**Apps, web and search**
- Open apps, websites and common folders (Downloads, Documents, Desktop, …)
- Google and YouTube search in your existing browser window
- Browser control: new/close/reopen/switch tabs, refresh, back/forward, zoom, bookmarks, history, downloads, incognito

**Windows control**
- Switch, close, maximize, restore and snap windows
- Minimize/restore everything, show desktop, task view, action center, fullscreen
- Protected-window list so it can't close PowerShell, Terminal, VS Code or itself
- Settings pages, Control Panel, Device Manager, Task Manager, elevated Command Prompt

**Input and media**
- Type text, press key combinations, copy/paste/cut, select all, undo/redo
- Screenshots, volume up/down/mute, media play/pause/next/previous
- Continuous auto-scroll (up/down/left/right/top/bottom) with a stop command
- Lock screen, shutdown/restart/sleep/hibernate, empty Recycle Bin
- System info and IP address read from the machine
- Instagram sections (messages, explore, reels, notifications, profile, saved) and next/previous reel

**HUD**
- Local web page (`hud.py` + `hud.html`) showing state, transcript and live voice levels

## Requirements

- Windows 10/11, Python 3.12
- NVIDIA GPU with 4 GB VRAM recommended (falls back to a smaller model if the 4-bit load fails)
- [espeak-ng](https://github.com/espeak-ng/espeak-ng/releases) (required by Kokoro)

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install torch --index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt
```

Install the espeak-ng `.msi` before the first run, otherwise Kokoro falls back to `pyttsx3`.

The first launch downloads the Whisper, Qwen and Kokoro models, so give it a few minutes.

## Run

```powershell
python jarvis.py            # voice mode: say "Hey Jarvis"
python jarvis.py --text     # type commands instead
python jarvis.py --no-hud   # without the HUD window
```

## Example commands

| Say this | What happens |
| --- | --- |
| "Hey Jarvis, open Chrome and search for biryani in Pune" | Opens a Google search |
| "Play lofi on YouTube" | Opens YouTube results for `lofi` |
| "Open Notepad and type hello world" | Launches Notepad and types the text |
| "Turn the volume down" | Presses the volume-down key |
| "Switch to Visual Studio Code" | Focuses the matching window |
| "Scroll down and keep going" | Continuous auto-scroll until "stop scrolling" |
| "Go to my Instagram messages" | Opens Instagram Direct |
| "Take a screenshot" | Saves a screenshot |
| "What's my IP address?" | Answers from the system |
| "Go to sleep" | Stops listening until the next wake word |

## Voice pipeline

Everything up to the LLM runs locally on the CPU:

| Stage | Detail |
| --- | --- |
| Wake word | openWakeWord, model `hey_jarvis` (ONNX). Triggers when the score passes `WAKE_THRESHOLD` (0.5); scores above 0.15 are printed so you can see how close it was |
| Calibration | The first second of mic audio sets the voice threshold to `max(300, mean × 2.5)` |
| Recording | 80 ms frames (`CHUNK = 1280` samples at 16 kHz). Speech has to start within 8 s (`FOLLOWUP_SECONDS`) when `ALWAYS_LISTEN` is on, 4 s otherwise; the recording ends after 1.2 s of quiet (`SILENCE_SECONDS`) and is capped at 10 s (`MAX_RECORD_SECONDS`) |
| Transcription | Whisper `small`, CPU, `int8` |
| Staying awake | `ALWAYS_LISTEN = True` keeps him taking commands after "Hey Jarvis" until a `SLEEP_PHRASES` match ("go to sleep", "stop listening", "standby", "sleep mode"); `False` falls back to the one-shot timeout |

The mic is stopped while Jarvis speaks so he cannot transcribe himself, and `--text` skips this whole
pipeline in favour of typed commands.

### Speech output

`speak()` prints the line, tells the HUD it is `speaking`, then plays it:

1. **Kokoro** (when installed) synthesises segment by segment; each one is played at 24 kHz and the call
   blocks until it finishes, so long replies play in order rather than overlapping.
2. **pyttsx3** is the fallback if Kokoro fails to import — it uses the system voice, so `TTS_VOICE`
   (`bm_george`) has no effect there.

A background thread streams loudness and frequency-band energy to the HUD while audio plays. Any playback
error is caught, printed as `(speech failed)`, and the HUD returns to `idle` — a failed sentence never
stops the action loop.

## Supported actions

48 whitelisted actions in total, grouped by what they touch:

| Area | Actions |
| --- | --- |
| Apps, sites, folders | `open_app`, `open_website`, `google_search`, `youtube_search`, `open_folder` |
| Typing & clipboard | `type_text`, `press_keys`, `clipboard_copy`, `clipboard_paste`, `clipboard_cut`, `select_all`, `undo`, `redo` |
| Windows | `switch_window`, `close_window`, `minimize_all`, `restore_all`, `window_maximize`, `window_restore`, `window_snap`, `show_desktop`, `toggle_fullscreen`, `task_view` |
| Browser tabs | `close_tab`, `switch_tab`, `reopen_tab`, `new_tab`, `browser_action`, `browser_type` |
| Scrolling | `scroll`, `stop_scroll` |
| Media & screen | `volume`, `media`, `screenshot`, `lock_screen`, `wait` |
| Instagram | `instagram`, `reel` |
| System | `system_power`, `empty_recycle_bin`, `system_info`, `ip_address`, `action_center`, `open_settings_page`, `open_control_panel`, `open_device_manager`, `open_task_manager`, `open_command_prompt_admin` |

The model never calls a function directly — it emits a JSON action that has to match a key in `ACTIONS`,
which is why a new ability is a three-step change (see "Adding a new ability").

## Action payloads

The shapes the model is allowed to send (from the `SYSTEM` prompt in `jarvis.py`):

| Action | Payload |
| --- | --- |
| `open_app` | `{"action": "open_app", "name": "<app name>"}` |
| `open_website` | `{"action": "open_website", "url": "<url>"}` |
| `google_search` | `{"action": "google_search", "query": "<text>"}` |
| `youtube_search` | `{"action": "youtube_search", "query": "<text>"}` |
| `open_folder` | `{"action": "open_folder", "path": "<downloads\|documents\|desktop\|pictures\|music\|videos\|full path>"}` |
| `type_text` | `{"action": "type_text", "text": "<text>", "search_box": <optional>, "enter": <optional>}` |
| `press_keys` | `{"action": "press_keys", "keys": ["ctrl", "t"]}` |
| `volume` | `{"action": "volume", "direction": "up\|down\|mute"}` |
| `media` | `{"action": "media", "key": "playpause\|nexttrack\|prevtrack"}` |
| `wait` | `{"action": "wait", "seconds": 2}` |
| `screenshot` / `lock_screen` | `{"action": "screenshot"}` — no extra fields |

Every reply also carries a `say` string, which is what gets spoken back to you.

## How replies are handled

`think()` allows 200 new tokens, takes the first `{...}` block it finds (greedy, across newlines) and parses
it as JSON. Everything downstream trusts that one shape, so the failure modes are short:

| Case | What happens |
| --- | --- |
| Reply has no `{...}` at all | The whole reply (first 200 characters) becomes `say`, with no actions |
| JSON is malformed | `run_command` catches it, turns the HUD to `error` and says "Sorry, I didn't catch that properly." |
| Unknown action name | Logged as `Unknown action:` and skipped — the remaining actions in the list still run |
| An action raises | Logged as `Action failed:`, he says "That one failed.", and carries on with the next action |

Time and date questions never reach the model: `quick_answer()` formats them from the system clock
("It's 7:42 PM.", "Today is Friday, 20 November 2026.") — unless the sentence also contains a command verb
like open/search/play, in which case it goes to the LLM as normal.

## Project layout

| File | Purpose |
| --- | --- |
| `jarvis.py` | Entry point: wake word, STT, LLM prompt, action dispatch, TTS, settings |
| `hud.py` | Serves the HUD on `127.0.0.1` and streams live events to it |
| `hud.html` | HUD front-end (no build step, opened by `hud.py`) |
| `requirements.txt` | Python dependencies |
| `.gitignore` | Keeps `.venv/`, caches, logs and backups out of git |

## Configuration

Everything configurable lives at the top of `jarvis.py`:

| Setting | Default | Notes |
| --- | --- | --- |
| `WAKE_WORD` | `hey_jarvis` | openWakeWord model name |
| `WAKE_THRESHOLD` | `0.5` | Lower = more sensitive, higher = fewer false triggers |
| `WHISPER_MODEL` | `small` | `base` is faster, `small` handles accents better |
| `LLM_MODEL` | `Qwen/Qwen2.5-3B-Instruct` | Loaded in 4-bit on GPU |
| `FALLBACK_MODEL` | `Qwen/Qwen2.5-1.5B-Instruct` | Used automatically if the 4-bit load fails |
| `TTS_VOICE` | `bm_george` | British male; try `bm_lewis` or `am_adam` |
| `ALWAYS_LISTEN` | `True` | Keep listening until "go to sleep" |
| `FOLLOWUP_SECONDS` | `8` | Follow-up window when `ALWAYS_LISTEN` is `False` |
| `INSTAGRAM_USER` | `""` | Your username, enables "my profile" and "saved posts" |
| `BROWSER` | `chrome` | `msedge` for Edge; used for searches and sites |

### Window names

"Switch to …" and "close …" are matched against the window title, and the spoken name is first
translated through `WINDOW_ALIASES`:

| You say | Matches window |
| --- | --- |
| `chrome`, `browser`, `google chrome` | Chrome |
| `vs code`, `vscode`, `code` | Visual Studio Code |
| `explorer`, `file explorer` | File Explorer |
| `edge`, `firefox`, `spotify`, `notepad`, `word`, `excel`, `powerpoint`, `terminal`, `settings` | Same names |

Anything not in the alias table is matched as a partial/fuzzy substring of the real window title.

### Scroll speed

"Scroll down a little / normally / a lot" map to `SCROLL_CLICKS` and `SCROLL_DELAY`:

| Amount | Wheel notches | Delay between notches |
| --- | --- | --- |
| `small` | 3 | 0.25 s |
| `medium` | 8 | 0.12 s |
| `large` | 16 | 0.05 s |

"Keep scrolling" / auto-scroll runs the same notches on a background thread until `stop_scroll`.

## HUD
The Stark-style window is plain HTML served by `hud.py` — no extra installs beyond `psutil` (optional):

- Binds `127.0.0.1` on the first free port in **8765–8775** and opens it in Edge app mode, fullscreen.
- Streams live events over SSE at `/events`: `state` (`idle` / `listening` / `thinking` / `speaking` / `error`) plus `level` for the voice meter.
- `/show` and `/hide` toggle the window; `python jarvis.py --no-hud` skips it entirely.
- `psutil` only powers the CPU/RAM panel — everything else works without it.

## Safety and privacy

- **Fixed action whitelist** — the LLM can only pick from `ACTIONS`; there is no arbitrary shell execution.
- **Protected windows** — `close_window` refuses anything matching `PROTECTED_TITLES` (PowerShell, Command Prompt, Windows Terminal, VS Code, Jarvis).
- **Elevated shell is explicit** — `open_command_prompt_admin` goes through the normal Windows UAC prompt, so it is always visible.
- **Panic switch** — `pyautogui.FAILSAFE` is on: slam the mouse into a screen corner to abort automated input.
- **Stays on the machine** — wake word, STT, the LLM and TTS all run locally; nothing is sent to a cloud API.

## Adding a new ability

1. Write a Python function that takes the action dict.
2. Add it to the `ACTIONS` dictionary.
3. Add its signature and an example to the `SYSTEM` prompt.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Wake word never fires, or fires too often | Tune `WAKE_THRESHOLD` (default `0.5` — lower is more sensitive, higher gives fewer false triggers) |
| "4-bit load ... failed" on startup | `bitsandbytes` is missing or the GPU can't fit the model — `pip install -U bitsandbytes`. Otherwise it continues on `Qwen2.5-1.5B-Instruct` automatically |
| No speech output / espeak-ng error | Install [espeak-ng](https://github.com/espeak-ng/espeak-ng/releases) (Windows `.msi`); `pyttsx3` is the optional fallback voice |
| Microphone hears nothing | Allow microphone access for the terminal in Windows privacy settings and make the right input device the default. Run `python jarvis.py --text` to test the rest of the pipeline |
| It keeps responding after a command | By design when `ALWAYS_LISTEN` is on — say "go to sleep" to stop |
| HUD doesn't open | Check the port printed at startup; `hud.py` picks the first free port on `127.0.0.1` |
| Model download is slow | First run only; models are cached under your Hugging Face cache directory |
| "Go to my Instagram profile / saved posts" does nothing | Those two need `INSTAGRAM_USER` set; otherwise it says "Set your Instagram username in the config first." |
| An app name isn't recognised | Add an alias to the `APPS` dict at the top of `jarvis.py` (e.g. `"notepad": "notepad"`), then restart |

## Notes

- Tested on an RTX 3050 (4 GB VRAM). Whisper and the wake word run on CPU (int8) so the LLM fits on the GPU.
- If VRAM is tight, switch `LLM_MODEL` to the 1.5B model or close other GPU apps.
- `hud.html.bak` is a local backup and is ignored by git.
