"""MCP server (stdio) so another agent (OpenCode, Hermes, Claude Code ...) can hand
screen work to the desktop assistant and talk to it while it works.

The user keeps chatting with their own agent; that agent forwards tasks and messages
here and reads back what the assistant does and asks. Starts server.py by itself when it is not
running (Mac: through the app, so the app holds the permissions). Every request carries the server's per-start token (local_auth.py,
read from the user-only file). No dependencies: JSON-RPC 2.0, one message per line.

OpenCode (~/.config/opencode/opencode.json):
  "mcp": {"servers": {"computer": {"type": "local", "command": ["<python>", "<desktop_agent>/mcp_server.py"],
          "timeout": {"startup": 90000, "catalog": 90000, "execution": 90000}}}}
Hermes (~/.hermes/config.yaml):
  mcp_servers: {computer: {command: "<python>", args: ["<desktop_agent>/mcp_server.py"], timeout: 90}}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import local_auth
import mac_rights

URL = "http://127.0.0.1:8765"
PROTOCOL_VERSION = "2025-06-18"
MAX_WAIT_SECONDS = 50
START_WAIT_SECONDS = 25
MAC_LAUNCHER = Path.home() / "Applications" / "hausverwalter.ai Computerbenutzung.app"
VERSION = "0.2.0"  # same as agent.VERSION (this file imports nothing heavy)

TEXT = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
NONE = {"type": "object", "properties": {}}
TOOLS = [
    {
        "name": "computer_auftrag",
        "description": "Gibt dem Computer-Assistenten einen Auftrag, den er am Bildschirm erledigt: klicken, tippen, Programme bedienen "
        "(z. B. PowerHaus, Fernzugriff, Browser). Er arbeitet im Hintergrund weiter. Danach computer_status aufrufen, bis er fertig ist "
        "oder etwas fragt. Läuft schon ein Auftrag, geht der Text als Nachricht an ihn. Startet sich selbst und prüft die Mac-Freigaben selbst: "
        "Einfach aufrufen. Nie den Nutzer bitten, etwas zu starten. Nie Freigaben aus deiner eigenen Shell testen, nie Freigaben für dein eigenes Programm verlangen.",
        "inputSchema": TEXT,
    },
    {
        "name": "computer_nachricht",
        "description": "Nachricht des Nutzers an den Computer-Assistenten, während er arbeitet oder nachdem er etwas gefragt hat: "
        "Korrektur, Antwort, Frage („Was machst du gerade?“). Gib den Wortlaut des Nutzers weiter.",
        "inputSchema": TEXT,
    },
    {
        "name": "computer_status",
        "description": "Wartet bis zu warte_sekunden auf Neues und liefert: Zustand (arbeitet, pausiert, fertig/wartet auf Antwort), "
        "die neuen Schritte seit dem letzten Aufruf und seine letzte Nachricht. Fragen des Assistenten an den Nutzer weitergeben.",
        "inputSchema": {"type": "object", "properties": {"warte_sekunden": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS}}},
    },
    {"name": "computer_pause", "description": "Hält den Computer-Assistenten an, bis computer_weiter kommt.", "inputSchema": NONE},
    {"name": "computer_weiter", "description": "Lässt den pausierten Computer-Assistenten weiterarbeiten.", "inputSchema": NONE},
    {"name": "computer_stopp", "description": "Bricht den aktuellen Auftrag des Computer-Assistenten ab.", "inputSchema": NONE},
]
NOT_RUNNING = "Der Computer-Assistent startet nicht. Nicht den Nutzer bitten: Rufe das Werkzeug in einer Minute noch einmal auf, dann Fehler an den hausverwalter.ai-Support melden."
INSTALL_COMMAND = (
    '$env:HV_KI = "1"; irm https://raw.githubusercontent.com/Powerprozesse-GmbH/hausverwalter.ai-computerbenutzung/main/install.ps1 | iex'
    if sys.platform == "win32"
    else "curl -fsSL https://raw.githubusercontent.com/Powerprozesse-GmbH/hausverwalter.ai-computerbenutzung/main/install.sh | HV_KI=1 bash"
)
NOT_INSTALLED = f"hausverwalter.ai Computerbenutzung ist nicht installiert. Selbst installieren, Befehl selbst ausführen: {INSTALL_COMMAND}"
KEY_MISSING = (
    "Noch kein Schlüssel. Schreib dem Nutzer zuerst wörtlich auf Deutsch, nicht übersetzen: „Fertig installiert. Damit das Programm für dich arbeiten darf, braucht es einen Schlüssel aus hausverwalter.ai. "
    "Ich öffne dir jetzt die Seite in <Name deines Standard-Browsers, z. B. Google Chrome>: Klick dort auf „Erstellen“ und dann auf „Kopieren“. Füge ihn nicht hier im Chat ein, schreib mir nur „fertig“.“ "
    "Öffne erst dann die Seite aus getSetupLink target api_keys. Warte. Nach „fertig“ schreib zuerst genau: „Perfekt. Jetzt öffnet sich ein kleines Fenster: "
    "Füge den Schlüssel dort ein und klick auf „Speichern“.“ Führe erst dann selbst aus: {} fenster . Danach computer_status."
)
WAITING = 'Der Computer-Assistent wartet auf eine Freigabe. Schreibe dem Nutzer zuerst genau diesen deutschen Satz, Wort für Wort, nicht übersetzen, nichts sonst: „{}“ Führe erst dann selbst aus: open "{}" . Warte auf „fertig“, dann computer_status. Sagt der Nutzer, er sieht nichts: Befehl noch einmal ausführen.'
REFUSED = "Der Computer-Assistent hat die Anfrage abgelehnt (Zugangs-Token passt nicht). Nicht den Nutzer bitten: In einer Minute noch einmal versuchen."


class Bridge:
    """Talks to server.py; remembers which log lines the calling agent has already seen."""

    def __init__(self, url: str = URL) -> None:
        self.url = url
        self.seen: int | None = None

    def call(self, name: str, args: dict) -> str:
        try:
            return self._call(name, args)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError):
            problem = self._start()
            return problem if problem else self._call(name, args)

    def _start(self) -> str | None:
        """Starts the assistant and waits for it. Returns the text for the agent if it cannot go on yet."""
        if not mac_rights.read_status():
            if not self._key_saved():
                return KEY_MISSING.format(f'"{sys.executable}" "{Path(__file__).with_name("api_key.py")}"')
            if not self._launch():
                return NOT_INSTALLED
        deadline = time.time() + START_WAIT_SECONDS
        while time.time() < deadline:
            waiting = mac_rights.read_status()
            if waiting:
                return WAITING.format(*waiting)
            try:
                self._state(None)
                return None
            except (urllib.error.URLError, OSError):
                time.sleep(1)
        return NOT_RUNNING

    def _key_saved(self) -> bool:
        check = [sys.executable, str(Path(__file__).with_name("api_key.py")), "pruefen"]
        return subprocess.run(check, capture_output=True).returncode == 0

    def _launch(self) -> bool:
        if sys.platform == "win32":
            folder = Path(__file__).resolve().parent
            subprocess.Popen([str(Path(sys.executable).with_name("pythonw.exe")), str(folder / "server.py")], cwd=folder, creationflags=subprocess.CREATE_NO_WINDOW)
            return True
        if not MAC_LAUNCHER.exists():
            return False
        subprocess.run(["open", str(MAC_LAUNCHER)], check=False)
        return True

    def _call(self, name: str, args: dict) -> str:
        if name in ("computer_auftrag", "computer_nachricht"):
            if self.seen is None:
                self.seen = self._state(None)["total"]  # report from this task on, not older history
            self._post("/send", {"text": args["text"]})
            return "Weitergegeben. Mit computer_status verfolgen."
        if name == "computer_status":
            return self._status(min(int(args.get("warte_sekunden", 20)), MAX_WAIT_SECONDS))
        path = {"computer_pause": "/pause", "computer_weiter": "/resume", "computer_stopp": "/stop"}[name]
        self._post(path, {})
        return {"/pause": "Pausiert.", "/resume": "Geht weiter.", "/stop": "Gestoppt."}[path]

    def _status(self, wait: int) -> str:
        since = self.seen if self.seen is not None else 0
        deadline = time.time() + wait
        state = self._state(since)
        while not state["log"] and state["running"] and not state["paused"] and time.time() < deadline:
            time.sleep(1)
            state = self._state(since)
        self.seen = state["total"]
        if state["paused"]:
            where = "pausiert"
        elif state["running"]:
            where = "arbeitet"
        else:
            where = "fertig oder wartet auf Antwort des Nutzers"
        problems = [line for line in state["log"] if line.startswith("Problem: ")]
        if problems and not state["running"]:
            return f"Auftrag abgebrochen. {problems[-1]} Sag dem Nutzer genau das in einfachen Worten, wiederhole den Auftrag nicht von selbst."
        lines = "\n".join(state["log"]) or "(nichts Neues)"
        return f"Zustand: {where}\nKosten bisher: {state['cost_usd']:.2f} USD\nNeue Schritte:\n{lines}\nLetzte Nachricht des Assistenten: {state['last_reply'] or '-'}"

    def _state(self, since: int | None) -> dict:
        query = f"?since={since}" if since is not None else ""
        request = urllib.request.Request(self.url + "/state" + query, headers=local_auth.headers())
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def _post(self, path: str, body: dict) -> None:
        headers = {"content-type": "application/json", **local_auth.headers()}
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), headers=headers)
        urllib.request.urlopen(request, timeout=5).read()


def handle(bridge: Bridge, message: dict) -> dict | None:
    """One JSON-RPC message in, the reply out (None for notifications)."""
    method, msg_id = message.get("method"), message.get("id")
    if msg_id is None:
        return None
    if method == "initialize":
        result = {
            "protocolVersion": message.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "hausverwalter-computer", "version": VERSION},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params", {})
        try:
            text, failed = bridge.call(params.get("name", ""), params.get("arguments") or {}), False
        except KeyError as exc:
            text, failed = f"Unbekanntes Werkzeug oder fehlendes Feld: {exc}", True
        except urllib.error.HTTPError as exc:
            text, failed = (REFUSED if exc.code in (401, 403) else f"Fehler des Assistenten: HTTP {exc.code}"), True
        except (urllib.error.URLError, OSError):
            text, failed = NOT_RUNNING, True  # started, but gone again
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> None:
    bridge = Bridge()
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(bridge, json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
