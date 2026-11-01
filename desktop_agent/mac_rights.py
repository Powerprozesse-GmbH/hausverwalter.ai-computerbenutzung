"""Mac only: gets the two permissions before the assistant starts, one switch at a time.

Runs from the app, so it checks the app that really needs them. Opens the exact
settings page and waits until the switch is on; the person does nothing else. While waiting, the
sentence for the person is in a file, so mcp_server.py can hand it to the calling AI agent.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import local_auth

SERVICES = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
SETTINGS = "x-apple.systempreferences:com.apple.preference.security?"
STATUS_FILE = "rechte-status"
MAX_WAIT_SECONDS = 600
POLL_SECONDS = 2


def status_path() -> Path:
    return local_auth.app_dir() / STATUS_FILE


def write_status(sentence: str, url: str = "") -> None:
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(f"{os.getpid()}\n{url}\n{sentence}", encoding="utf-8")


def clear_status() -> None:
    status_path().unlink(missing_ok=True)


def read_status() -> tuple[str, str] | None:
    """(sentence for the person, settings link), only while the process that wrote them is still alive."""
    try:
        pid, url, sentence = status_path().read_text(encoding="utf-8").split("\n", 2)
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return None
    return sentence, url


def host_app() -> str:
    return "hausverwalter.ai Computerbenutzung"


def screen_ok() -> bool:
    """A real capture from a new process: the only check that does not go stale while waiting."""
    with tempfile.TemporaryDirectory() as tmp:
        return subprocess.run(["screencapture", "-x", "-m", "-C", str(Path(tmp, "probe.png"))], capture_output=True).returncode == 0


def access_ok() -> bool:
    """From a new process: AXIsProcessTrusted stays false in a process that started before the switch went on."""
    code = f"import ctypes, sys; sys.exit(0 if ctypes.cdll.LoadLibrary({SERVICES!r}).AXIsProcessTrusted() else 1)"
    return subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0


def ask_screen() -> None:
    """Puts the app into the settings list, so the person never needs the + button."""
    ctypes.cdll.LoadLibrary(SERVICES).CGRequestScreenCaptureAccess()


def ask_access() -> None:
    services, foundation = ctypes.cdll.LoadLibrary(SERVICES), ctypes.cdll.LoadLibrary(FOUNDATION)
    key = ctypes.c_void_p.in_dll(services, "kAXTrustedCheckOptionPrompt")
    true = ctypes.c_void_p.in_dll(foundation, "kCFBooleanTrue")
    foundation.CFDictionaryCreate.restype = ctypes.c_void_p
    foundation.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                              ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    options = foundation.CFDictionaryCreate(None, ctypes.byref(key), ctypes.byref(true), 1, None, None)
    services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    services.AXIsProcessTrustedWithOptions(options)


def wait_for(name: str, anchor: str, check, ask, opening: str = "", extra: str = "") -> bool:
    if check():
        return True
    app = host_app()
    sentence = f"{opening}Ich öffne dir jetzt die Einstellung „{name}“. Schalte dort den Schalter bei „{app}“ ein und schreib mir „fertig“.{extra}"
    write_status(sentence, SETTINGS + anchor)
    print(sentence, flush=True)
    ask()
    # no "open" here: a background app cannot bring the settings to the front, the AI's own shell can
    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        if check():
            return True
        time.sleep(POLL_SECONDS)
    return False


def main() -> int:
    steps = [("Bildschirmaufnahme", "Privacy_ScreenCapture", screen_ok, ask_screen, "Damit ich deinen Bildschirm sehen kann, brauche ich eine Erlaubnis vom Mac. ", " Fragt der Mac nach: „Beenden & erneut öffnen“ klicken."),
             ("Bedienungshilfen", "Privacy_Accessibility", access_ok, ask_access, "Perfekt. Damit ich Maus und Tastatur bewegen kann, brauche ich noch eine Erlaubnis. ", "")]
    try:
        for step in steps:
            if not wait_for(*step):
                print(f"„{step[0]}“ wurde nicht eingeschaltet. Zum erneuten Versuch „hausverwalter.ai Computerbenutzung“ starten.")
                return 1
    finally:
        clear_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
