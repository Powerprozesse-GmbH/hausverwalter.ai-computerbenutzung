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
LAUNCHER="$HOME/Applications/hausverwalter Computer.app"
KEY_URL="${HV_KEY_URL:-https://app.hausverwalter.ai/settings/api-keys}"
KEY_HINT='Ich habe die Schlüssel-Seite von hausverwalter.ai geöffnet. Klick dort auf „Erstellen“ und „Kopieren“. Danach geht es von selbst weiter.'
KEY_PROMPT='Perfekt. Füge den Schlüssel jetzt hier ein (⌘V) und klick auf „Speichern“.'

say() { printf '\n==> %s\n' "$1"; }

clipboard_id() { pbpaste 2>/dev/null | shasum; }

# The key window must come up only after the copy: clicking the browser would hide a window that is already open.
wait_for_copied_key() {
  local before tries=0 hint
  before="$(clipboard_id)"
  osascript -e activate -e "display dialog \"$KEY_HINT\" with title \"hausverwalter Computer\" buttons {\"OK\"} default button \"OK\" giving up after 10" >/dev/null 2>&1 &
  hint=$!
  while [ "$tries" -lt 600 ]; do
    if [ "$(clipboard_id)" != "$before" ] && pbpaste 2>/dev/null | LC_ALL=C grep -Eq '^[A-Za-z0-9_-]{16,}$'; then break; fi
    sleep 1
    tries=$((tries + 1))
  done
  kill "$hint" 2>/dev/null || true
}

main() {
  [ "$(uname -s)" = Darwin ] || { echo "Dieses Skript ist für macOS. Unter Windows: install.ps1"; exit 1; }
  mkdir -p "$BASE" "$HOME/Applications"
  lsof -ti tcp:8765 | xargs kill 2>/dev/null || true  # an update replaces the running program
  pkill -f "desktop_agent/mac_rights.py|python mac_rights.py" 2>/dev/null || true

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
  elif [ -n "${HV_KI:-}" ]; then
    echo "Noch kein Schlüssel: Die KI fragt im Chat danach."
  else
    open "$KEY_URL" || true
    sleep 2
    wait_for_copied_key
    # hidden dialog: the key never shows up in a terminal, a log or the AI agent that runs this script
    until
      KEY="$(osascript -e activate -e "text returned of (display dialog \"$KEY_PROMPT\" with title \"hausverwalter Computer\" default answer \"\" with hidden answer buttons {\"Abbrechen\", \"Speichern\"} default button \"Speichern\" cancel button \"Abbrechen\")")" \
        || { echo "Abgebrochen. Zum Einrichten das Skript erneut ausführen."; exit 1; }
      printf '%s\n' "$KEY" | "$PY" "$APP/desktop_agent/api_key.py" einrichten --stdin
    do :; done
    unset KEY
  fi

  say "5/5 Startsymbol anlegen"
  # A real app without a window: no Terminal window, and the Mac settings list it by its own name.
  rm -rf "$LAUNCHER" "$HOME/Applications/hausverwalter Computer.command"
  mkdir -p "$LAUNCHER/Contents/MacOS"
  cat > "$LAUNCHER/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>ai.hausverwalter.computer</string>
<key>CFBundleName</key><string>hausverwalter Computer</string>
<key>CFBundleExecutable</key><string>start</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
</dict></plist>
EOF
  cat > "$LAUNCHER/Contents/MacOS/start" <<EOF
#!/bin/bash
cd "$APP/desktop_agent"
export HV_PROVIDER=hausverwalter HV_VIA_STARTSYMBOL=1
"$PY" mac_rights.py >> "$BASE/programm.log" 2>&1 || exit 1
"$PY" server.py >> "$BASE/programm.log" 2>&1
EOF
  chmod +x "$LAUNCHER/Contents/MacOS/start"
  codesign --force --deep -s - "$LAUNCHER" >/dev/null 2>&1 || true
  "$PY" "$APP/desktop_agent/mcp_eintragen.py" "$PY" "$APP/desktop_agent/mcp_server.py" || true
  if "$PY" "$APP/desktop_agent/api_key.py" pruefen >/dev/null 2>&1; then open "$LAUNCHER"; fi

  cat <<EOF

Fertig, der Assistent startet jetzt und fragt selbst nach den zwei Mac-Freigaben, eine nach der anderen.
Später starten: Doppelklick auf „hausverwalter Computer“ (Ordner Programme in Ihrem Benutzerordner).

KI-Agenten: Das Werkzeug „computer_status“ startet ihn von selbst. OpenCode, Claude-App und Claude Code sind eingetragen, falls vorhanden.
Hermes (~/.hermes/config.yaml) von Hand:
mcp_servers:
  computer:
    command: "$PY"
    args: ["$APP/desktop_agent/mcp_server.py"]
    timeout: 90
EOF
}

main "$@"  # whole script read before anything runs (safe with curl | bash)
