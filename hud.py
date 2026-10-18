"""Tony-Stark-style HUD for Jarvis. Serves hud.html and streams live events to it (no extra installs)."""
import json, queue, subprocess, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
try:
    import psutil          # optional: pip install psutil (CPU/RAM panel)
except ImportError:
    psutil = None

PAGE = Path(__file__).with_name("hud.html")
_clients, _lock = [], threading.Lock()
_hud_proc = None          # subprocess.Popen for the Edge/HUD window
_hud_port = None          # port the HUD server is listening on
_hud_visible = False      # whether the HUD window is currently open
_hud_srv = None           # ThreadingHTTPServer instance


def emit(kind, **data):
    if not _clients:
        return
    msg = "data: " + json.dumps({"type": kind, **data}) + "\n\n"
    with _lock:
        for q in _clients:
            q.put(msg)


def state(s):  emit("state", v=s)               # idle | listening | thinking | speaking | error
def level(v):  emit("level", v=round(float(v), 3))


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/show"):
            _show_hud()
            self._send(b'{"ok":true}', "application/json")
        elif self.path.startswith("/hide"):
            _hide_hud()
            self._send(b'{"ok":true}', "application/json")
        elif self.path.startswith("/events"):
            q = queue.Queue()
            with _lock: _clients.append(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    try: m = q.get(timeout=15)
                    except queue.Empty: m = ": ping\n\n"
                    self.wfile.write(m.encode()); self.wfile.flush()
            except Exception:
                pass
            finally:
                with _lock: _clients.remove(q)
        elif self.path.startswith("/stats"):
            d = {}
            if psutil:
                d = {"cpu": psutil.cpu_percent(), "ram": psutil.virtual_memory().percent}
                b = psutil.sensors_battery()
                if b: d["bat"] = b.percent
            self._send(json.dumps(d).encode(), "application/json")
        else:
            self._send(PAGE.read_bytes(), "text/html; charset=utf-8")


def _show_hud():
    global _hud_proc, _hud_visible
    if _hud_visible:
        return
    url = f"http://127.0.0.1:{_hud_port}"
    try:
        _hud_proc = subprocess.Popen(
            f'start "" msedge --app={url} --start-fullscreen', shell=True
        )
    except Exception:
        webbrowser.open(url)
    _hud_visible = True
    print("HUD shown:", url)


def _hide_hud():
    global _hud_proc, _hud_visible
    if not _hud_visible:
        return
    if _hud_proc is not None:
        try:
            _hud_proc.terminate()
        except Exception:
            pass
        _hud_proc = None
    _hud_visible = False
    print("HUD hidden")


def stop():
    """Terminate the HUD window and stop the HTTP server."""
    global _hud_srv
    _hide_hud()
    if _hud_srv is not None:
        try:
            _hud_srv.shutdown()
        except Exception:
            pass
        _hud_srv = None


def start(fullscreen=True):
    global _hud_port, _hud_proc, _hud_visible, _hud_srv
    for port in range(8765, 8776):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler); break
        except OSError:
            continue
    else:
        print("HUD: no free port"); return None
    _hud_port = port
    _hud_srv = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    _show_hud()
    print("HUD:", url)
    return srv, port
