"""Registers the MCP server „computer“ in the AI programs on this computer; the installers call it.

Only touches programs that are installed, only adds this one entry, keeps a backup next to the file.
Stdlib only. Usage: mcp_eintragen.py <python> <mcp_server.py>
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT_MS = {"startup": 90000, "catalog": 90000, "execution": 90000}


def claude_config(home: Path) -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "Claude" / "claude_desktop_config.json"
    return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def add_entry(path: Path, keys: list[str], entry: dict) -> str:
    """Sets the entry below keys. Returns "neu", "unverändert", "kein Programm" or "Datei nicht lesbar"."""
    if not path.parent.is_dir():
        return "kein Programm"
    config = {}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return "Datei nicht lesbar"
    node = config
    for key in keys:
        node = node.setdefault(key, {})
    if node.get("computer") == entry:
        return "unverändert"
    node["computer"] = entry
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak-hv"))
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "neu"


def main(python: str, script: str, home: Path | None = None) -> None:
    home = home or Path.home()
    results = {
        "OpenCode": add_entry(home / ".config" / "opencode" / "opencode.json", ["mcp", "servers"],
                              {"type": "local", "command": [python, script], "timeout": TIMEOUT_MS}),
        "Claude-App": add_entry(claude_config(home), ["mcpServers"], {"command": python, "args": [script]}),
    }
    claude = shutil.which("claude")
    if claude:
        done = subprocess.run([claude, "mcp", "add", "--scope", "user", "computer", "--", python, script], capture_output=True)
        results["Claude Code"] = "neu" if done.returncode == 0 else "unverändert"
    for program, result in results.items():
        print(f"{program}: {result}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
