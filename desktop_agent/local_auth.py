"""Per-start token for the local server (127.0.0.1:8765), so websites and other users cannot drive it.

server.py writes a fresh random token at every start to a file only this user can read
(mode 600; on Windows the user profile's own ACL). The page, the overlay and the MCP bridge
send it in the X-HV-Token header. Stdlib only, mcp_server.py imports it too.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

HEADER = "X-HV-Token"
APP_NAME = "hausverwalter-computer"
TOKEN_FILE = "server-token"


def app_dir() -> Path:
    """Per-user data folder: ~/Library/Application Support/… (macOS), %LOCALAPPDATA%\\… (Windows)."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME


def token_path() -> Path:
    override = os.environ.get("HV_TOKEN_FILE")  # tests
    return Path(override) if override else app_dir() / TOKEN_FILE


def new_token(path: Path | None = None) -> str:
    """Creates a new token file readable only by this user and returns the token."""
    path = path or token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.unlink()  # a fresh file, so it never keeps wider rights from before
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as file:
        file.write(token)
    return token


def read_token(path: Path | None = None) -> str | None:
    try:
        return (path or token_path()).read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def remove_token(token: str, path: Path | None = None) -> None:
    """Deletes the token file on exit, unless a newer server has replaced it."""
    path = path or token_path()
    if read_token(path) == token:
        try:
            path.unlink()
        except OSError:
            pass


def headers(path: Path | None = None) -> dict[str, str]:
    """Header dict for a request to the local server (empty when no server has written a token)."""
    token = read_token(path)
    return {HEADER: token} if token else {}
