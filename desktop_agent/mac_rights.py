"""Mac only: gets the two permissions before the assistant starts, one switch at a time.

Runs from the start entry, so it checks the app that really needs them (Terminal). Opens the exact
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
DIALOG_SECONDS = 15


def status_path() -> Path:
    return local_auth.app_dir() / STATUS_FILE


def write_status(sentence: str) -> None:
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(f"{os.getpid()}\n{sentence}", encoding="utf-8")


def clear_status() -> None:
    status_path().unlink(missing_ok=True)


def read_status() -> str | None:
    """The sentence for the person, only while the process that wrote it is still alive."""
    try:
        pid, sentence = status_path().read_text(encoding="utf-8").split("\n", 1)
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return None
    return sentence


def host_app() -> str:
    return {"iTerm.app": "iTerm"}.get(os.environ.get("TERM_PROGRAM", ""), "Terminal")


def screen_ok() -> bool:
    """A real capture from a new process: the only check that does not go stale while waiting."""
    with tempfile.TemporaryDirectory() as tmp:
        return subprocess.run(["screencapture", "-x", "-m", "-C", str(Path(tmp, "probe.png"))], capture_output=True).returncode == 0


def access_ok() -> bool:
    return bool(ctypes.cdll.LoadLibrary(SERVICES).AXIsProcessTrusted())


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


def show_dialog(sentence: str) -> subprocess.Popen:
    script = f'display dialog "{sentence}" with title "hausverwalter Computer" buttons {{"OK"}} default button "OK" giving up after {DIALOG_SECONDS}'
    return subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for(name: str, anchor: str, check, ask, opening: str = "", extra: str = "") -> bool:
    if check():
        return True
    app = host_app()
    sentence = f"{opening}Ich habe dir „{name}“ geöffnet. Bitte den Schalter bei „{app}“ einschalten und „fertig“ schreiben.{extra}"
    write_status(sentence)
    print(sentence, flush=True)
    ask()
    subprocess.run(["open", SETTINGS + anchor], check=False)
    dialog = show_dialog(sentence.replace('"', "'"))
    deadline = time.time() + MAX_WAIT_SECONDS
    try:
        while time.time() < deadline:
            if check():
                return True
            time.sleep(POLL_SECONDS)
    finally:
        dialog.terminate()
    return False


def main() -> int:
    steps = [("Bildschirmaufnahme", "Privacy_ScreenCapture", screen_ok, ask_screen, "", " Fragt der Mac nach: „Beenden & erneut öffnen“ klicken."),
             ("Bedienungshilfen", "Privacy_Accessibility", access_ok, ask_access, "Perfekt. ", "")]
    try:
        for step in steps:
            if not wait_for(*step):
                print(f"„{step[0]}“ wurde nicht eingeschaltet. Zum erneuten Versuch „hausverwalter Computer“ starten.")
                return 1
    finally:
        clear_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
