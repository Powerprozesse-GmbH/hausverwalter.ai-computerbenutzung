#!/bin/bash
# hausverwalter Computer – Installation auf dem Mac, ohne Administratorrechte:
#   curl -fsSL https://raw.githubusercontent.com/Powerprozesse-GmbH/hausverwalter-computer/main/install.sh | bash
# Legt alles unter ~/Library/Application Support/hausverwalter-computer ab (Python 3.12 über uv),
# fragt den hausverwalter.ai-API-Schlüssel in einem Fenster mit verdeckter Eingabe ab und speichert
# ihn im Schlüsselbund. Erneut ausführen = aktualisieren; der Schlüssel bleibt
# (neu eingeben: HV_NEUER_SCHLUESSEL=1 voranstellen).
set -euo pipefail

REPO="https://github.com/Powerprozesse-GmbH/hausverwalter-computer"
BRANCH="${HV_BRANCH:-main}"
BASE="$HOME/Library/Application Support/hausverwalter-computer"
APP="$BASE/app"
VENV="$BASE/venv"
PY="$VENV/bin/python"
LAUNCHER="$HOME/Applications/hausverwalter Computer.command"
KEY_PROMPT='Bitte den API-Schlüssel aus hausverwalter.ai einfügen (Einstellungen › API-Schlüssel & MCP). Er wird nur im Schlüsselbund dieses Macs gespeichert.'

say() { printf '\n==> %s\n' "$1"; }

main() {
  [ "$(uname -s)" = Darwin ] || { echo "Dieses Skript ist für macOS. Unter Windows: install.ps1"; exit 1; }
  mkdir -p "$BASE" "$HOME/Applications"

  say "1/5 uv einrichten (lädt Python, ohne Administratorrechte)"
  UV="$(command -v uv || true)"
  if [ -z "$UV" ]; then
    UV="$HOME/.local/bin/uv"
    [ -x "$UV" ] || curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
  fi

  say "2/5 Programm laden"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar -xz -C "$TMP"
  rm -rf "$APP"
  mv "$TMP"/hausverwalter-computer-* "$APP"

  say "3/5 Python 3.12 und Pakete installieren (beim ersten Mal 1–2 Minuten)"
  rm -rf "$VENV"
  "$UV" venv --quiet --python 3.12 --python-preference only-managed "$VENV"
  "$UV" pip install --quiet --python "$PY" -r "$APP/requirements.txt"

  say "4/5 hausverwalter.ai-API-Schlüssel"
  if [ -z "${HV_NEUER_SCHLUESSEL:-}" ] && "$PY" "$APP/desktop_agent/api_key.py" pruefen >/dev/null 2>&1; then
    echo "Schlüssel ist schon gespeichert."
  else
    # hidden dialog: the key never shows up in a terminal, a log or the AI agent that runs this script
    KEY="$(osascript -e "text returned of (display dialog \"$KEY_PROMPT\" with title \"hausverwalter Computer\" default answer \"\" with hidden answer buttons {\"Abbrechen\", \"Speichern\"} default button \"Speichern\" cancel button \"Abbrechen\")")" \
      || { echo "Abgebrochen. Zum Einrichten das Skript erneut ausführen."; exit 1; }
    printf '%s\n' "$KEY" | "$PY" "$APP/desktop_agent/api_key.py" einrichten --stdin
    unset KEY
  fi

  say "5/5 Startsymbol anlegen"
  cat > "$LAUNCHER" <<EOF
#!/bin/bash
# Startet den hausverwalter Computer. Fenster offen lassen, solange er arbeiten soll; Schließen beendet ihn.
cd "$APP/desktop_agent"
export HV_PROVIDER=hausverwalter
exec "$PY" server.py
EOF
  chmod +x "$LAUNCHER"
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" || true
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" || true
  open -R "$LAUNCHER" || true
  echo "Bitte in beiden geöffneten Einstellungen „Terminal“ einschalten (fehlt es: mit + aus Programme › Dienstprogramme hinzufügen), damit der Assistent den Bildschirm sehen und Maus und Tastatur bedienen darf."

  cat <<EOF

Fertig. Starten: Doppelklick auf „hausverwalter Computer“ (Ordner Programme in Ihrem Benutzerordner).
Oben auf dem Bildschirm erscheint dann eine Leiste mit Pause und Stopp.

Für KI-Agenten (MCP-Server „computer“), Eintrag in der Konfiguration ergänzen:

OpenCode (~/.config/opencode/opencode.json):
{
  "mcp": {
    "servers": {
      "computer": {
        "type": "local",
        "command": ["$PY", "$APP/desktop_agent/mcp_server.py"],
        "timeout": {"startup": 90000, "catalog": 90000, "execution": 90000}
      }
    }
  }
}

Hermes (~/.hermes/config.yaml):
mcp_servers:
  computer:
    command: "$PY"
    args: ["$APP/desktop_agent/mcp_server.py"]
    timeout: 90
EOF
}

main "$@"  # whole script read before anything runs (safe with curl | bash)
