"""Small local window for the desktop agent: http://127.0.0.1:8765 (this computer only).

Also the control point for the overlay (overlay.py on macOS, overlay_win.py on Windows:
glowing screen edge, Pause/Stopp) and for other agents (mcp_server.py: OpenCode, Hermes).
HV_NO_BROWSER=1 skips opening the page, HV_NO_OVERLAY=1 skips the overlay.

Security: binds to 127.0.0.1 only. Every request except the static page needs the per-start
token (header X-HV-Token, file readable only by this user, see local_auth.py; the page gets it
in the URL fragment). Requests with a foreign Origin or Host are refused, so websites (also via
DNS rebinding) cannot drive it. Screenshots and texts stay in memory.
Stopp is instant: the state says "not running" at once (the overlay hides within one poll),
the worker drops the model call in flight and runs no further action.
"""

from __future__ import annotations

import atexit
import hmac
import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import agent
import local_auth

PORT = 8765
TENANT = os.environ.get("HV_TENANT", "intern")
BUDGET_USD = float(os.environ.get("HV_BUDGET_USD", "20"))
MAX_LINES = 5000  # log lines kept in memory; long runs drop the oldest
STOP_JOIN_SECONDS = 10  # a stopped worker finishes its current action at most
MAX_BODY_BYTES = 1_000_000
# Provider comes from HV_PROVIDER (agent.Session reads it), shown in the page title.
WHERE = {
    "hausverwalter": "hausverwalter.ai, Claude über AWS Frankfurt",
    "bedrock": "AWS Frankfurt",
    "vertex": f"Google Cloud {os.environ.get('HV_GCP_REGION', 'europe-west1')}",
    "azure": "Azure Schweden, GPT-5.6 Luna",
    "tensorx": f"TensorX (EU), {agent.TENSORX_MODEL}",
}

PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><title>hausverwalter.ai Assistent</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font:16px -apple-system,"Segoe UI",system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}
main{max-width:720px;margin:24px auto;padding:0 16px}
h1{font-size:20px} textarea{width:100%;box-sizing:border-box;height:90px;font:inherit;padding:10px;border:1px solid #c9ced8;border-radius:8px}
.row{display:flex;gap:8px;margin:10px 0;flex-wrap:wrap} button{font:inherit;padding:8px 16px;border-radius:8px;border:0;cursor:pointer}
#go{background:#1f6feb;color:#fff} #stop{background:#d1242f;color:#fff} #pause,#new{background:#e3e6eb}
#log{background:#fff;border:1px solid #e3e6eb;border-radius:8px;padding:12px;height:360px;overflow:auto;white-space:pre-wrap;font-size:14px}
#status{color:#57606a;font-size:14px}
</style></head><body><main>
<h1>Assistent (KI über __WHERE__)</h1>
<textarea id="text" placeholder="Was soll ich am Computer erledigen? Während ich arbeite: Nachricht an mich."></textarea>
<div class="row"><button id="go">Senden</button><button id="pause">Pause</button><button id="stop">Stopp</button><button id="new">Neue Aufgabe</button></div>
<div id="status"></div><div id="log"></div>
</main><script>
const $=id=>document.getElementById(id);
const hash=new URLSearchParams(location.hash.slice(1)).get('t');
if(hash){sessionStorage.setItem('t',hash);history.replaceState(null,'',location.pathname);}
const token=sessionStorage.getItem('t')||'';
const H={'content-type':'application/json','X-HV-Token':token};
let paused=false;
async function post(path,body){await fetch(path,{method:'POST',headers:H,body:JSON.stringify(body||{})});refresh();}
$('go').onclick=()=>{const t=$('text').value.trim();if(t){post('/send',{text:t});$('text').value='';}};
$('pause').onclick=()=>post(paused?'/resume':'/pause');
$('stop').onclick=()=>post('/stop'); $('new').onclick=()=>post('/new');
async function refresh(){const r=await fetch('/state',{headers:H});
 if(r.status===401){$('status').textContent='Kein Zugang: bitte den Assistenten über das Startmenü bzw. Programme neu öffnen.';return;}
 const s=await r.json(); paused=s.paused;
 $('log').textContent=s.log.join('\\n'); $('log').scrollTop=1e9;
 $('status').textContent=(s.paused?'Pausiert. ':s.running?'Arbeitet … ':'Bereit. ')+'Kosten bisher ca. '+s.cost_usd.toFixed(4)+' USD ('
  +s.tokens_in+' / '+s.tokens_out+' Tokens)'+(s.errors?' · Fehler: '+s.errors:'');
 $('pause').textContent=s.paused?'Weiter':'Pause'; $('pause').disabled=!s.running;}
setInterval(refresh,1000); refresh();
</script></body></html>"""


class State:
    """The current session, its worker thread and the log (newest MAX_LINES lines)."""

    def __init__(self, make_session: Callable[[Callable[[str], None]], Any]) -> None:
        self.make_session = make_session
        self.lines: list[str] = []
        self.dropped = 0  # lines removed from the front; `total` and `since` count them, so readers never skip
        self.thread: threading.Thread | None = None
        self.session = make_session(self.log)

    def log(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > MAX_LINES:
            extra = len(self.lines) - MAX_LINES
            del self.lines[:extra]
            self.dropped += extra

    @property
    def running(self) -> bool:
        """False right after Stopp, even while the worker still winds down."""
        return bool(self.thread and self.thread.is_alive()) and not self.session.stop_event.is_set()

    def send(self, text: str) -> None:
        """New task or answer when idle; while a task runs it goes to the model as a message."""
        if not text:
            return
        self.log(f"Du: {text}")
        if self.running:
            self.session.inbox.put(text)
            return
        if not self._worker_gone():
            self.log("Der letzte Schritt läuft noch. Bitte gleich noch einmal senden.")
            return
        self.session.stop_event.clear()  # running is true from the first moment
        self.thread = threading.Thread(target=self._safe_send, args=(self.session, text), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.thread and self.thread.is_alive():
            self.session.stop()

    def new(self) -> None:
        self.stop()
        self._worker_gone()
        self.dropped += len(self.lines)
        self.lines.clear()
        self.thread = None
        self.session = self.make_session(self.log)

    def _worker_gone(self) -> bool:
        """Waits for a stopped worker; it drops its model call at once and ends after the current action."""
        if self.thread and self.thread.is_alive():
            self.thread.join(STOP_JOIN_SECONDS)
        return not (self.thread and self.thread.is_alive())

    def _safe_send(self, session: Any, text: str) -> None:
        try:
            session.send(text)
        except Exception as exc:
            session.error_count += 1
            self.log(f"Fehler: {exc}")

    def snapshot(self, since: int | None) -> dict:
        s, screen = self.session, self.session.screen
        lines, dropped = list(self.lines), self.dropped
        click = getattr(screen, "last_click", None)
        replies = [line for line in lines if line.startswith("KI: ")]
        running = self.running
        return {
            "log": lines[max(0, since - dropped):] if since is not None else lines[-300:],
            "total": dropped + len(lines),
            "running": running,
            "paused": running and s.pause_event.is_set(),
            "acting": running and s.acting,
            "cost_usd": s.cost_micro_usd / 1e6,
            "tokens_in": s.tokens_in,
            "tokens_out": s.tokens_out,
            "errors": s.error_count,
            "last_reply": replies[-1][len("KI: "):] if replies else "",
            "last_step": lines[-1].removeprefix("KI: ") if lines else "",
            "click": {"x": click[0], "y": click[1], "t": click[2]} if click else None,
        }


class Handler(BaseHTTPRequestHandler):
    """Needs self.server.state (State) and self.server.token (str), see make_server()."""

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if not self._allowed(public=url.path == "/"):
            return
        if url.path == "/state":
            since = parse_qs(url.query).get("since")
            self._send(200, "application/json", json.dumps(self.server.state.snapshot(int(since[0]) if since else None)))
        elif url.path == "/":
            provider = self.server.state.session.provider
            self._send(200, "text/html; charset=utf-8", PAGE.replace("__WHERE__", WHERE.get(provider, provider)))
        else:
            self._send(404, "text/plain", "")

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY_BYTES:
            self._send(413, "text/plain", "")
            return
        body = self.rfile.read(length)  # read before any answer, else the client sees a reset instead of 401/403
        if not self._allowed(public=False):
            return
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            self._send(400, "text/plain", "")
            return
        state = self.server.state
        if self.path == "/send":
            state.send(str(data.get("text", "")).strip())
        elif self.path == "/pause":
            state.session.pause()
        elif self.path == "/resume":
            state.session.resume()
        elif self.path == "/stop":
            state.stop()
        elif self.path == "/new":
            state.new()
        else:
            self._send(404, "text/plain", "")
            return
        self._send(204, "text/plain", "")

    def do_OPTIONS(self) -> None:
        self._send(403, "text/plain", "")  # no CORS: other origins get no preflight approval

    def _allowed(self, public: bool) -> bool:
        """Host and Origin must be this server (no websites, no DNS rebinding); the token unless public."""
        port = self.server.server_address[1]
        ours = (f"127.0.0.1:{port}", f"localhost:{port}")
        origin = self.headers.get("origin")
        if self.headers.get("host") not in ours or (origin is not None and origin not in tuple(f"http://{o}" for o in ours)):
            self._send(403, "text/plain", "")
            return False
        token = self.server.token
        if not public and not (token and hmac.compare_digest(self.headers.get(local_auth.HEADER, ""), token)):
            self._send(401, "text/plain", "")
            return False
        return True

    def _send(self, code: int, kind: str, body: str) -> None:
        self.send_response(code)
        self.send_header("content-type", kind)
        self.send_header("cache-control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body.encode())

    def log_message(self, *args) -> None:
        pass


def make_server(state: State, token: str, port: int = PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.state, server.token = state, token
    return server


def make_session(log: Callable[[str], None]) -> agent.Session:
    """Mac or Windows screen by platform; Windows-only modules are never imported on the Mac and vice versa."""
    if sys.platform == "win32":
        from win_control import WinScreen as Screen
    else:
        from mac_control import MacScreen as Screen
    session = agent.Session(tenant_id=TENANT, screen=Screen(), log=log)
    if session.con is not None:  # internal providers: local tenant budget
        agent.ensure_tenant(session.con, TENANT, BUDGET_USD)
    return session


def start_overlay() -> None:
    name = "overlay_win.py" if sys.platform == "win32" else "overlay.py"
    overlay = subprocess.Popen([sys.executable, str(Path(__file__).with_name(name))])
    atexit.register(overlay.terminate)


def restart_through_launcher() -> bool:
    """Mac: started from an AI program's shell, the permissions would count for that program, not for Terminal."""
    if sys.platform != "darwin" or os.environ.get("HV_VIA_STARTSYMBOL") or os.environ.get("HV_PROVIDER", "hausverwalter") != "hausverwalter":
        return False
    launcher = Path.home() / "Applications" / "hausverwalter Computer.command"
    if not launcher.exists():
        return False
    subprocess.run(["open", str(launcher)], check=False)
    return True


def main() -> None:
    if restart_through_launcher():
        return
    try:
        state = State(make_session)
    except agent.MissingKey as exc:
        sys.exit(str(exc))
    server = make_server(state, "")  # bind first: a second instance fails here, before it replaces the running one's token
    server.token = token = local_auth.new_token()
    atexit.register(local_auth.remove_token, token)
    if not os.environ.get("HV_NO_OVERLAY"):
        start_overlay()
    if os.environ.get("HV_BROWSER"):  # customers talk to their AI; the page is only for debugging
        webbrowser.open(f"http://127.0.0.1:{PORT}/#t={token}")
    print(f"Assistent läuft: http://127.0.0.1:{PORT}. Dieses Fenster offen lassen, Schließen beendet ihn.")
    server.serve_forever()


if __name__ == "__main__":
    main()
