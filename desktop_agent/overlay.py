"""Shows people that the assistant is at work: glowing screen edges, a ring at every
click, and a small panel (top centre) with the current step, Pause/Weiter and Stopp.

Started by server.py, polls its /state with the per-start token (local_auth.py). All windows are hidden from screenshots
(sharingType none), so Claude never sees or clicks them. While an action runs the
panel lets clicks through, so the assistant cannot hit it by accident.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.request

import AppKit
import objc
import Quartz
from Foundation import NSMakeRect, NSObject, NSTimer
from PyObjCTools import AppHelper

import local_auth

URL = "http://127.0.0.1:8765"
POLL_SECONDS = 0.15
FRAME_SECONDS = 1 / 30
GLOW_PX = 26
RING_SECONDS = 0.7
WORKING = (1.0, 0.27, 0.23)  # red
PAUSED = (1.0, 0.72, 0.10)  # amber
PANEL_W, PANEL_H = 480, 64
WINDOW_LEVEL = AppKit.NSScreenSaverWindowLevel
EVERYWHERE = (
    AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
    | AppKit.NSWindowCollectionBehaviorStationary
    | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
    | AppKit.NSWindowCollectionBehaviorIgnoresCycle
)

# Latest /state from the server (written by the poll thread, read on the main thread).
STATE: dict = {"running": False, "paused": False, "acting": False, "last_reply": "", "click": None}
# What the glow views draw: colour, strength 0..1, click ring in AppKit global coordinates.
LOOK: dict = {"color": WORKING, "alpha": 0.0, "ring": None}


def _rgba(rgb, alpha):
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _cg(rgb, alpha):
    """CGColor for layers (NSColor.CGColor() only gives an untyped pointer in PyObjC)."""
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


class GlowView(AppKit.NSView):
    def drawRect_(self, rect):
        bounds, alpha = self.bounds(), LOOK["alpha"]
        if alpha > 0:
            for i in range(GLOW_PX):
                _rgba(LOOK["color"], alpha * (1 - i / GLOW_PX) ** 2).set()
                path = AppKit.NSBezierPath.bezierPathWithRect_(AppKit.NSInsetRect(bounds, i + 0.5, i + 0.5))
                path.setLineWidth_(1.0)
                path.stroke()
        if LOOK["ring"]:
            gx, gy, progress = LOOK["ring"]
            origin = self.window().frame().origin
            x, y, radius = gx - origin.x, gy - origin.y, 10 + 28 * progress
            _rgba(LOOK["color"], 0.95 * (1 - progress)).set()
            path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - radius, y - radius, 2 * radius, 2 * radius))
            path.setLineWidth_(3.5)
            path.stroke()


class FirstClickButton(AppKit.NSButton):
    def acceptsFirstMouse_(self, event):
        return True  # one click is enough even though the panel never becomes active


class Controller(NSObject):
    def init(self):
        self = objc.super(Controller, self).init()
        self.glows = [self._glow_window(screen) for screen in AppKit.NSScreen.screens()]
        self._build_panel()
        self.started = time.time()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(FRAME_SECONDS, self, "tick:", None, True)
        return self

    # --- windows ------------------------------------------------------------------------

    @objc.python_method
    def _glow_window(self, screen):
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            screen.frame(), AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False
        )
        win.setFrame_display_(screen.frame(), False)
        self._overlay_window(win)
        win.setIgnoresMouseEvents_(True)
        win.setContentView_(GlowView.alloc().initWithFrame_(NSMakeRect(0, 0, screen.frame().size.width, screen.frame().size.height)))
        win.orderFrontRegardless()
        return win

    @objc.python_method
    def _overlay_window(self, win):
        win.setOpaque_(False)
        win.setBackgroundColor_(AppKit.NSColor.clearColor())
        win.setHasShadow_(False)
        win.setLevel_(WINDOW_LEVEL)
        win.setCollectionBehavior_(EVERYWHERE)
        win.setSharingType_(AppKit.NSWindowSharingNone)  # not in screenshots, Claude never sees it

    @objc.python_method
    def _build_panel(self):
        visible = AppKit.NSScreen.mainScreen().visibleFrame()
        frame = NSMakeRect(
            visible.origin.x + (visible.size.width - PANEL_W) / 2,
            visible.origin.y + visible.size.height - PANEL_H - 10,
            PANEL_W,
            PANEL_H,
        )
        style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(frame, style, AppKit.NSBackingStoreBuffered, False)
        self._overlay_window(panel)
        panel.setLevel_(WINDOW_LEVEL + 1)
        panel.setHasShadow_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)

        box = AppKit.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, PANEL_H))
        box.setWantsLayer_(True)
        box.layer().setBackgroundColor_(_cg((0.08, 0.08, 0.08), 0.92))
        box.layer().setCornerRadius_(14)
        box.layer().setBorderWidth_(1.5)
        box.layer().setBorderColor_(_cg(WORKING, 0.9))
        panel.setContentView_(box)

        self.title = self._label(NSMakeRect(16, 34, 290, 20), 14, True)
        self.step = self._label(NSMakeRect(16, 12, 290, 18), 12, False)
        self.pause_button = self._button("Pause", NSMakeRect(318, 16, 72, 32), "togglePause:")
        self.stop_button = self._button("Stopp", NSMakeRect(396, 16, 72, 32), "stop:")
        self.stop_button.setBezelColor_(_rgba(WORKING, 1.0))
        for view in (self.title, self.step, self.pause_button, self.stop_button):
            box.addSubview_(view)
        self.panel, self.box = panel, box

    @objc.python_method
    def _label(self, frame, size, bold):
        label = AppKit.NSTextField.labelWithString_("")
        label.setFrame_(frame)
        label.setTextColor_(AppKit.NSColor.whiteColor())
        label.setFont_(AppKit.NSFont.boldSystemFontOfSize_(size) if bold else AppKit.NSFont.systemFontOfSize_(size))
        label.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        return label

    @objc.python_method
    def _button(self, title, frame, action):
        button = FirstClickButton.alloc().initWithFrame_(frame)
        button.setTitle_(title)
        button.setBezelStyle_(AppKit.NSBezelStyleRounded)
        button.setTarget_(self)
        button.setAction_(action)
        return button

    # --- buttons --------------------------------------------------------------------------

    def togglePause_(self, sender):
        _post("/resume" if STATE.get("paused") else "/pause")

    def stop_(self, sender):
        _post("/stop")

    # --- every frame ---------------------------------------------------------------------

    def tick_(self, timer):
        if os.getppid() == 1:  # server is gone
            AppKit.NSApp().terminate_(None)
            return
        running, paused = STATE.get("running"), STATE.get("paused")
        LOOK["color"] = PAUSED if paused else WORKING
        if not running:
            LOOK["alpha"] = 0.0
        elif paused:
            LOOK["alpha"] = 0.75
        else:
            LOOK["alpha"] = 0.6 + 0.3 * math.sin((time.time() - self.started) * 3.0)
        LOOK["ring"] = self._ring()
        for win in self.glows:
            win.contentView().setNeedsDisplay_(True)

        if running and not self.panel.isVisible():
            self.panel.orderFrontRegardless()
        elif not running and self.panel.isVisible():
            self.panel.orderOut_(None)
        self.panel.setIgnoresMouseEvents_(bool(STATE.get("acting")))
        self.box.layer().setBorderColor_(_cg(LOOK["color"], 0.9))
        self.title.setStringValue_("Assistent pausiert" if paused else "Assistent arbeitet am Computer")
        self.step.setStringValue_(STATE.get("last_step") or "…")
        self.pause_button.setTitle_("Weiter" if paused else "Pause")

    @objc.python_method
    def _ring(self):
        click = STATE.get("click")
        if not click:
            return None
        progress = (time.time() - click["t"]) / RING_SECONDS
        if not 0 <= progress <= 1:
            return None
        main_height = AppKit.NSScreen.screens()[0].frame().size.height
        return click["x"], main_height - click["y"], progress  # Quartz (top-left) to AppKit (bottom-left)


def _post(path: str) -> None:
    def send():
        try:
            request = urllib.request.Request(URL + path, data=b"{}", headers={"content-type": "application/json", **local_auth.headers()})
            urllib.request.urlopen(request, timeout=2).read()
        except OSError:
            pass

    threading.Thread(target=send, daemon=True).start()


def _poll() -> None:
    while True:
        try:
            request = urllib.request.Request(URL + "/state?since=999999999", headers=local_auth.headers())
            with urllib.request.urlopen(request, timeout=2) as response:
                STATE.update(json.load(response))
        except (OSError, ValueError):
            STATE.update(running=False, paused=False, acting=False)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon, never takes focus
    threading.Thread(target=_poll, daemon=True).start()
    controller = Controller.alloc().init()
    AppHelper.runEventLoop()
