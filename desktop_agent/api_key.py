"""The customer's hausverwalter.ai API key, kept in the OS key store via keyring
(macOS Keychain, Windows Credential Manager). Never in a file, never printed or logged.

  python api_key.py einrichten           asks for the key (hidden input) and stores it
  python api_key.py einrichten --stdin   reads it from stdin (installers pipe it from a hidden dialog)
  python api_key.py fenster           asks in a small window with hidden input and stores it
  python api_key.py pruefen              exit code 0 if a key is stored
  python api_key.py loeschen             removes it
"""

from __future__ import annotations

import getpass
import subprocess
import sys

SERVICE = "hausverwalter-computer"
ACCOUNT = "api-key"
MIN_LENGTH = 16
PROMPT = "hausverwalter.ai API-Schlüssel einfügen (die Eingabe bleibt unsichtbar), dann Enter: "


class InvalidKey(Exception):
    """Readable German problem with the key; the message never contains the key."""


def load() -> str | None:
    import keyring

    return keyring.get_password(SERVICE, ACCOUNT) or None


def save(key: str) -> None:
    key = key.strip()
    if len(key) < MIN_LENGTH or any(c.isspace() for c in key):
        raise InvalidKey("Das sieht nicht nach einem API-Schlüssel aus. Bitte in hausverwalter.ai › Einstellungen › API-Schlüssel & MCP kopieren.")
    import keyring

    keyring.set_password(SERVICE, ACCOUNT, key)


def delete() -> None:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except PasswordDeleteError:
        pass


WINDOW_TEXT = "Schlüssel hier einfügen"
MAC_WINDOW = f"""text returned of (display dialog "{WINDOW_TEXT}" with title "hausverwalter Computer" default answer "" with hidden answer buttons {{"Abbrechen", "Speichern"}} default button "Speichern" cancel button "Abbrechen")"""
WINDOWS_WINDOW = f"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Windows.Forms.Form; $f.Text = "hausverwalter Computer"; $f.ClientSize = New-Object System.Drawing.Size(400, 110)
$f.StartPosition = "CenterScreen"; $f.TopMost = $true; $f.FormBorderStyle = "FixedDialog"; $f.MaximizeBox = $false; $f.MinimizeBox = $false
$l = New-Object System.Windows.Forms.Label; $l.Text = "{WINDOW_TEXT}"; $l.SetBounds(12, 10, 376, 20)
$b = New-Object System.Windows.Forms.TextBox; $b.UseSystemPasswordChar = $true; $b.SetBounds(12, 34, 376, 24)
$o = New-Object System.Windows.Forms.Button; $o.Text = "Speichern"; $o.DialogResult = "OK"; $o.SetBounds(300, 70, 88, 28)
$f.Controls.AddRange(@($l, $b, $o)); $f.AcceptButton = $o; $f.Add_Shown({{ $f.Activate(); $b.Focus() }})
if ($f.ShowDialog() -eq "OK") {{ [Console]::Out.Write($b.Text) }}
"""


def ask_in_window() -> str:
    """Hidden input in a small window, so the key never reaches a chat, a terminal or a log."""
    if sys.platform == "win32":
        command = ["powershell", "-NoProfile", "-Command", WINDOWS_WINDOW]
    else:
        command = ["osascript", "-e", "activate", "-e", MAC_WINDOW]
    return subprocess.run(command, capture_output=True, text=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "einrichten"
    try:
        if command in ("einrichten", "setup"):
            key = sys.stdin.readline() if "--stdin" in args else getpass.getpass(PROMPT)
            save(key)
            print("Schlüssel sicher gespeichert (Schlüsselbund des Betriebssystems).")
        elif command == "fenster":
            key = ask_in_window()
            if not key:
                print("Abgebrochen.", file=sys.stderr)
                return 1
            save(key)
            print("Schlüssel sicher gespeichert (Schlüsselbund des Betriebssystems).")
        elif command in ("pruefen", "check"):
            stored = load() is not None
            print("Schlüssel ist gespeichert." if stored else "Kein Schlüssel gespeichert.")
            return 0 if stored else 1
        elif command in ("loeschen", "delete"):
            delete()
            print("Schlüssel gelöscht.")
        else:
            print(__doc__)
            return 2
    except InvalidKey as exc:
        print(exc, file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Abgebrochen.", file=sys.stderr)
        return 1
    except ImportError:
        print("Das Paket „keyring“ fehlt. Bitte die Installation erneut ausführen.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
