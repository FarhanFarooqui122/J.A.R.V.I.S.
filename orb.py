"""
Floating JARVIS desktop orb - always-on-top toggle icon.
Requires: PySide6

A compact circular orb that floats above all windows on the Windows desktop.
- Left-click: show/restore or minimize the main JARVIS HUD
- Right-click: context menu (Open JARVIS, Minimize HUD, Exit JARVIS)
- Draggable: reposition anywhere; last position saved to settings.json
- Voice-reactive: glows bright cyan when JARVIS speaks (real audio amplitude
  from the existing _pulse() pipeline via SSE), gentle pulse when listening,
  subtle glow when idle.

Connects to the same SSE /events stream used by hud.html, so it receives the
actual level/band data from Kokoro TTS playback - no fake animation.
"""

import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QRadialGradient, QAction
from PySide6.QtWidgets import QApplication, QWidget, QMenu

SETTINGS_FILE = Path(__file__).with_name("settings.json")
ORB_SIZE = 60
ORB_RADIUS = ORB_SIZE // 2 - 4


class JarvisOrb(QWidget):
    def __init__(self, hud_base_url="http://127.0.0.1:8765"):
        super().__init__()
        self.hud_base_url = hud_base_url.rstrip("/")
        self.hud_visible = False

        # Voice state from SSE
        self.state = "idle"      # idle | listening | speaking | error
        self.level = 0.0         # smoothed audio level (0-1)
        self.target_level = 0.0  # target from latest SSE event
        self.pulse_phase = 0.0   # animation phase

        self.drag_pos = None
        self.pos_x, self.pos_y = self._load_position()

        self._init_ui()
        self._start_sse_listener()

    # ------------------------------------------------------------------ setup

    def _init_ui(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(ORB_SIZE, ORB_SIZE)
        self.move(self.pos_x, self.pos_y)

        # Animation timer (~33 FPS)
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self._animate)
        self.anim_timer.start(30)

    def _load_position(self):
        try:
            if SETTINGS_FILE.exists():
                with open(SETTINGS_FILE) as f:
                    data = json.load(f)
                return int(data.get("orb_x", -1)), int(data.get("orb_y", -1))
        except Exception:
            pass
        # Default: right edge, vertically centered
        screen = QApplication.primaryScreen().geometry()
        return screen.width() - 90, screen.height() // 2 - ORB_SIZE // 2

    def _save_position(self):
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump({"orb_x": self.pos_x, "orb_y": self.pos_y}, f)
        except Exception:
            pass

    def _start_sse_listener(self):
        """Connect to the HUD's SSE /events stream for real audio levels."""
        def listen():
            backoff = 1.0
            while True:
                try:
                    url = f"{self.hud_base_url}/events"
                    req = urllib.request.Request(
                        url,
                        headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        backoff = 1.0
                        for line in resp:
                            line = line.decode("utf-8", errors="replace").strip()
                            if line.startswith("data:"):
                                try:
                                    payload = json.loads(line[5:].strip())
                                    self._handle_sse_event(payload)
                                except (json.JSONDecodeError, IndexError, KeyError):
                                    pass
                except Exception:
                    # HUD server not reachable yet; retry with backoff
                    threading.Event().wait(min(backoff, 10.0))
                    backoff = min(backoff * 1.5, 10.0)

        t = threading.Thread(target=listen, daemon=True)
        t.start()

    def _handle_sse_event(self, payload):
        mtype = payload.get("type")
        if mtype == "state":
            self.state = payload.get("v", "idle")
            self.update()
        elif mtype == "level":
            self.target_level = float(payload.get("v", 0.0))
            self.update()

    # ----------------------------------------------------------- painting

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        center = self.rect().center()
        radius = ORB_RADIUS

        # Smooth level interpolation
        self.level += (self.target_level - self.level) * 0.18

        # State colors
        if self.state == "speaking":
            glow_c = QColor(0, 242, 255)
            border_c = QColor(0, 242, 255)
            base_alpha = 140
        elif self.state == "listening":
            glow_c = QColor(70, 255, 215)
            border_c = QColor(70, 255, 215)
            base_alpha = 110
        else:  # idle / error
            glow_c = QColor(0, 200, 255)
            border_c = QColor(0, 200, 255)
            base_alpha = 70

        # ---- Outer neon glow ----
        glow_radius = radius + 10 + int(14 * self.level)
        gradient = QRadialGradient(center, glow_radius)
        gradient.setColorAt(0, QColor(glow_c.red(), glow_c.green(), glow_c.blue(),
                                      int(90 * self.level + 30)))
        gradient.setColorAt(1, QColor(0, 0, 0, 0))
        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(center, glow_radius, glow_radius)

        # ---- Border ring ----
        pen = QPen(border_c, 3)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(center, radius, radius)

        # ---- Center fill ----
        center_color = QColor(border_c.red(), border_c.green(), border_c.blue(), base_alpha)
        painter.setBrush(QBrush(center_color))
        inner_r = int(radius * 0.42)
        painter.drawEllipse(center, inner_r, inner_r)

        # ---- Speaking pulse rings ----
        if self.state == "speaking" and self.level > 0.12:
            ring_pen = QPen(QColor(0, 242, 255, int(110 * self.level)), 2)
            painter.setPen(ring_pen)
            painter.setBrush(Qt.NoBrush)
            pulse_r = radius + int(10 * self.level) + int(12 * (self.pulse_phase % 1.0))
            painter.drawEllipse(center, pulse_r, pulse_r)

    def _animate(self):
        self.pulse_phase += 0.07
        if self.pulse_phase > 6.28318:
            self.pulse_phase = 0.0
        self.update()

    # ----------------------------------------------------------- dragging

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        elif event.button() == Qt.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self.drag_pos is not None:
            new_pos = event.globalPosition().toPoint() - self.drag_pos
            self.move(new_pos)
            self.pos_x = self.x()
            self.pos_y = self.y()
            self._save_position()
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = None

    # ----------------------------------------------------------- context menu

    def _show_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: rgba(3, 7, 10, 0.94);
                color: #00f2ff;
                border: 1px solid rgba(0, 242, 255, 0.45);
                border-radius: 8px;
                padding: 6px;
                font-family: 'Segoe UI', Orbitron, Consolas, monospace;
                font-size: 12px;
            }
            QMenu::item {
                background-color: transparent;
                padding: 8px 24px;
                border-radius: 6px;
            }
            QMenu::item:selected {
                background-color: rgba(0, 242, 255, 0.18);
            }
            QMenu::separator {
                height: 1px;
                background: rgba(0, 242, 255, 0.25);
                margin: 6px 0;
            }
        """)

        open_action = QAction("Open JARVIS", self)
        open_action.triggered.connect(self._toggle_hud)
        menu.addAction(open_action)

        hide_action = QAction("Minimize HUD", self)
        hide_action.triggered.connect(self._hide_hud)
        menu.addAction(hide_action)

        menu.addSeparator()

        exit_action = QAction("Exit JARVIS", self)
        exit_action.triggered.connect(self._exit_jarvis)
        menu.addAction(exit_action)

        menu.exec_(pos)

    # ----------------------------------------------------------- HUD control

    def _toggle_hud(self):
        if self.hud_visible:
            self._hide_hud()
        else:
            self._show_hud()

    def _http_get(self, path, timeout=2):
        try:
            urllib.request.urlopen(f"{self.hud_base_url}{path}", timeout=timeout)
            return True
        except Exception as e:
            print(f"Orb: {path} failed: {e}")
            return False

    def _show_hud(self):
        self.hud_visible = self._http_get("/show")

    def _hide_hud(self):
        ok = self._http_get("/hide")
        if ok:
            self.hud_visible = False

    def _exit_jarvis(self):
        QApplication.quit()

    def closeEvent(self, event):
        # Ignore window close; only Exit JARVIS should shut down
        event.ignore()


def run_orb(hud_base_url="http://127.0.0.1:8765"):
    """Entry point when run as a standalone process."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    orb = JarvisOrb(hud_base_url)
    orb.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else "8765"
    run_orb(f"http://127.0.0.1:{port}")
