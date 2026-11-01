"""Windows version of overlay.py: glowing red screen edge (amber when paused), a ring at every
click, and a small panel (top centre) with the current step, Pause/Weiter and Stopp.

Started by server.py, polls its /state with the per-start token (local_auth.py). Both windows
are excluded from screen capture (SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE, Windows 10
2004+), so the model never sees or clicks them. The edge lets every click through, the panel
does too while an action runs; neither takes the keyboard focus. tkinter + ctypes, nothing to install.
"""

from __future__ import annotations

import ctypes
import json
import math
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request

import local_auth
import win_control

URL = "http://127.0.0.1:8765"
POLL_SECONDS = 0.15
FRAME_MS = 33
EDGE_PX = 8
RING_SECONDS = 0.7
WORKING = "#ff453a"  # red
PAUSED = "#ffb81a"  # amber
KEY = "#010203"  # this colour is see-through in the edge window
PANEL_W, PANEL_H = 480, 64  # at 100 % scaling
GONE_AFTER_FAILURES = 40  # ~6 s without an answer from the server: it is gone, quit

GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x20, 0x80, 0x80000, 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x11

# Latest /state from the server (written by the poll thread, read on the Tk thread).
STATE: dict = {"running": False, "paused": False, "acting": False, "last_step": "", "click": None, "gone": False}
user32 = ctypes.WinDLL("user32")


def _hwnd(window: tk.Misc) -> int:
    return user32.GetAncestor(window.winfo_id(), GA_ROOT)


def _set_style(hwnd: int, click_through: bool) -> None:
    """Never takes the focus, no taskbar button; click_through lets mouse clicks fall to the window below."""
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED
    style = style | WS_EX_TRANSPARENT if click_through else style & ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def _hide_from_capture(hwnd: int) -> None:
    if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        # Windows before 10 2004: the overlay shows up in screenshots, the assistant still works
        print("Hinweis: Rahmen und Leiste erscheinen auf Bildschirmfotos (Windows zu alt).")


class Overlay:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.started = time.time()
        self.scale = (user32.GetDpiForSystem() if hasattr(user32, "GetDpiForSystem") else 96) / 96
        self.w, self.h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

        root.overrideredirect(True)
        root.geometry(f"{self.w}x{self.h}+0+0")
        root.configure(bg=KEY)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", KEY)
        root.attributes("-alpha", 0.0)
        self.canvas = tk.Canvas(root, width=self.w, height=self.h, bg=KEY, highlightthickness=0)
        self.canvas.pack()
        half = EDGE_PX / 2
        self.edge = self.canvas.create_rectangle(half, half, self.w - half, self.h - half, outline=WORKING, width=EDGE_PX)
        self.inner = self.canvas.create_rectangle(EDGE_PX + 2, EDGE_PX + 2, self.w - EDGE_PX - 2, self.h - EDGE_PX - 2, outline=WORKING, width=2)
        self.ring = self.canvas.create_oval(0, 0, 0, 0, outline=WORKING, width=3, state="hidden")

        self.panel = tk.Toplevel(root, bg="#141414")
        self.panel.overrideredirect(True)
        self.panel.attributes("-topmost", True)
        self.panel.attributes("-alpha", 0.0)
        pw, ph = round(PANEL_W * self.scale), round(PANEL_H * self.scale)
        self.panel.geometry(f"{pw}x{ph}+{(self.w - pw) // 2}+{round(10 * self.scale)}")
        self.title = tk.Label(self.panel, text="", fg="white", bg="#141414", font=("Segoe UI", 10, "bold"), anchor="w")
        self.step = tk.Label(self.panel, text="", fg="#d0d0d0", bg="#141414", font=("Segoe UI", 9), anchor="w")
        self.pause_button = tk.Button(self.panel, text="Pause", command=self.toggle_pause, relief="flat", bg="#3a3a3a", fg="white")
        self.stop_button = tk.Button(self.panel, text="Stopp", command=lambda: _post("/stop"), relief="flat", bg=WORKING, fg="white")
        self.title.place(relx=0.03, rely=0.12, relwidth=0.62)
        self.step.place(relx=0.03, rely=0.55, relwidth=0.62)
        self.pause_button.place(relx=0.67, rely=0.22, relwidth=0.15, relheight=0.56)
        self.stop_button.place(relx=0.83, rely=0.22, relwidth=0.15, relheight=0.56)

        root.update()  # maps both windows, so their top-level handles exist
        self.edge_hwnd, self.panel_hwnd = _hwnd(root), _hwnd(self.panel)
        _set_style(self.edge_hwnd, click_through=True)
        self.panel_click_through = True
        _set_style(self.panel_hwnd, click_through=True)
        for hwnd in (self.edge_hwnd, self.panel_hwnd):
            _hide_from_capture(hwnd)
        self.texts = ("", "")

    def toggle_pause(self) -> None:
        _post("/resume" if STATE.get("paused") else "/pause")

    def tick(self) -> None:
        if STATE.get("gone"):
            self.root.destroy()
            return
        running, paused = bool(STATE.get("running")), bool(STATE.get("paused"))
        colour = PAUSED if paused else WORKING
        if not running:
            alpha = 0.0  # invisible, and click-through below: idle costs the user nothing
        elif paused:
            alpha = 0.85
        else:
            alpha = 0.6 + 0.3 * math.sin((time.time() - self.started) * 3.0)
        self.root.attributes("-alpha", alpha)
        for item in (self.edge, self.inner, self.ring):
            self.canvas.itemconfigure(item, outline=colour)
        self._draw_ring()

        self.panel.attributes("-alpha", 0.95 if running else 0.0)
        click_through = not running or bool(STATE.get("acting"))
        if click_through != self.panel_click_through:
            _set_style(self.panel_hwnd, click_through)
            self.panel_click_through = click_through
        texts = ("Assistent pausiert" if paused else "Assistent arbeitet am Computer", STATE.get("last_step") or "…")
        if texts != self.texts:
            self.title.configure(text=texts[0])
            self.step.configure(text=texts[1][:90])
            self.pause_button.configure(text="Weiter" if paused else "Pause")
            self.texts = texts
        self.root.after(FRAME_MS, self.tick)

    def _draw_ring(self) -> None:
        click = STATE.get("click")
        progress = (time.time() - click["t"]) / RING_SECONDS if click else -1
        if not 0 <= progress <= 1:
            self.canvas.itemconfigure(self.ring, state="hidden")
            return
        radius = (10 + 28 * progress) * self.scale
        x, y = click["x"], click["y"]  # physical pixels, same as the canvas (both DPI aware)
        self.canvas.coords(self.ring, x - radius, y - radius, x + radius, y + radius)
        self.canvas.itemconfigure(self.ring, state="normal")


def _post(path: str) -> None:
    def send() -> None:
        try:
            request = urllib.request.Request(URL + path, data=b"{}", headers={"content-type": "application/json", **local_auth.headers()})
            urllib.request.urlopen(request, timeout=2).read()
        except OSError:
            pass

    threading.Thread(target=send, daemon=True).start()


def _poll() -> None:
    failures = 0
    while True:
        try:
            request = urllib.request.Request(URL + "/state?since=999999999", headers=local_auth.headers())
            with urllib.request.urlopen(request, timeout=2) as response:
                STATE.update(json.load(response))
            failures = 0
        except (OSError, ValueError) as exc:
            STATE.update(running=False, paused=False, acting=False)
            # 401 = server restarted with a new token (read again next time); refused = server gone
            failures = 0 if isinstance(exc, urllib.error.HTTPError) else failures + 1
            if failures >= GONE_AFTER_FAILURES:
                STATE["gone"] = True
                return
        time.sleep(POLL_SECONDS)


def main() -> None:
    win_control.dpi_aware()  # before Tk creates a window: physical pixels like the screenshots
    previous = user32.GetForegroundWindow()
    root = tk.Tk()
    overlay = Overlay(root)
    if previous:
        user32.SetForegroundWindow(previous)  # creating the windows must not take the focus from the user's program
    threading.Thread(target=_poll, daemon=True).start()
    root.after(FRAME_MS, overlay.tick)
    root.mainloop()


if __name__ == "__main__":
    main()
