# hausverwalter Computer

Die KI bedient Ihren Computer. Sie schreiben, was erledigt werden soll, zum Beispiel „Buche die Rechnung von Firma Müller in PowerHaus“, und der Assistent klickt und tippt selbst. Das klappt auch in Programmen auf dem Terminalserver (Remotedesktop, Citrix), denn er arbeitet mit dem, was auf dem Bildschirm zu sehen ist.

Sie sehen jeden Schritt: Ein roter Rahmen leuchtet am Bildschirmrand, oben steht, was er gerade tut, und Sie können jederzeit **Pause** oder **Stopp** drücken.

Für Mac und Windows. Teil von [hausverwalter.ai](https://hausverwalter.ai).
## Was er macht

- Er macht ein Bildschirmfoto, Claude (die KI von Anthropic) entscheidet den nächsten Schritt, dann bewegt er Maus und Tastatur.
- Texte auf dem Bildschirm erkennt er direkt auf Ihrem Computer (Apple Vision bzw. Windows-Texterkennung), damit jeder Klick genau sitzt.
- Er arbeitet selbstständig, auch an langen Aufgaben über Stunden. Er fragt nur vor Schritten, die sich nicht rückgängig machen lassen (senden, löschen, bezahlen, abschicken, überschreiben), und wenn Zugangsdaten fehlen.
- Passwörter, PINs und Bankdaten gibt er nie selbst ein.
- Geht etwas schief, das Schaden anrichten kann, hört er sofort auf und sagt genau, was passiert ist.

## Datenschutz (DSGVO)

- **Wohin gehen die Bildschirmfotos?** Nur zu Claude, über hausverwalter.ai und AWS Bedrock in Frankfurt (EU-Profil von AWS: Verarbeitung nur in Rechenzentren in der EU).
- **Was wird gespeichert?** Nichts: keine Bildschirmfotos, keine erkannten Texte. Auf Ihrem Computer liegen sie nur im Arbeitsspeicher (auf dem Mac kurz als temporäre Datei, die sofort gelöscht wird). hausverwalter.ai zählt nur die Aufrufe für die Abrechnung, ohne Inhalte. AWS Bedrock speichert Anfragen nicht und nutzt sie nicht zum Training.
- **Ihr API-Schlüssel** liegt im Schlüsselspeicher des Betriebssystems (Mac: Schlüsselbund, Windows: Anmeldeinformationsverwaltung), nie in einer Datei.
- **Das Bedienfenster** ist nur auf diesem Computer erreichbar (127.0.0.1) und bei jedem Start mit einem neuen Zugangscode geschützt, den nur Ihr Benutzerkonto lesen kann. Webseiten und andere Benutzer können den Assistenten nicht steuern.
- **Kein Tracking:** Mitgeschickt werden nur die Programmversion und eine zufällige Auftragsnummer. Auf Ihrem Computer zählt er nur, wie viele Fehler es gab, ohne Inhalt.

## Was Sie brauchen

- Ein hausverwalter.ai-Konto mit KI-Guthaben und einen API-Schlüssel: hausverwalter.ai › Einstellungen › API-Schlüssel & MCP.
- Einen Mac mit aktuellem macOS oder einen PC mit Windows 10 (ab Version 2004) oder Windows 11.
- Keine Administratorrechte.

## Installieren

Am einfachsten bitten Sie Ihren KI-Agenten (zum Beispiel OpenCode oder Hermes), diesen Befehl auszuführen. Oder Sie kopieren ihn selbst in das Programm „Terminal“ (Mac) bzw. „PowerShell“ (Windows).

**Mac:**

```bash
curl -fsSL https://raw.githubusercontent.com/Powerprozesse-GmbH/hausverwalter-computer/main/install.sh | bash
```

**Windows:**

```powershell
irm https://raw.githubusercontent.com/Powerprozesse-GmbH/hausverwalter-computer/main/install.ps1 | iex
```

Was dabei passiert:

1. Python wird in Ihren Benutzerordner geladen (über das Werkzeug „uv“), ohne Administratorrechte und ohne Ihr System zu verändern.
2. Das Programm kommt nach `~/Library/Application Support/hausverwalter-computer` (Mac) bzw. `%LOCALAPPDATA%\hausverwalter-computer` (Windows).
3. Ein kleines Fenster fragt nach Ihrem API-Schlüssel. Die Eingabe ist verdeckt, auch Ihr KI-Agent sieht den Schlüssel nicht.
4. Es entsteht das Programm „hausverwalter Computer“ (Mac: Ordner „Programme“ in Ihrem Benutzerordner, Windows: Startmenü). Es läuft ohne eigenes Fenster.
5. Ihr KI-Agent wird eingetragen (OpenCode, Claude-App, Claude Code, falls vorhanden), und der Assistent startet.
6. Nur Mac: Der Assistent öffnet nacheinander **Bildschirmaufnahme** und **Bedienungshilfen** und sagt Ihnen, welchen Schalter Sie einschalten. Danach macht er von selbst weiter.

Zum Aktualisieren den Befehl einfach noch einmal ausführen. Der Schlüssel bleibt gespeichert.

## Starten und bedienen

1. Starten: Doppelklick auf **hausverwalter Computer** (Mac) bzw. Startmenü › **hausverwalter Computer** (Windows). Ihr KI-Agent startet es sonst von selbst.
2. Sie sprechen nur mit Ihrem KI-Agenten: Er gibt dem Assistenten die Aufträge.
3. Während er arbeitet, können Sie Ihrem KI-Agenten weiter schreiben: Korrekturen („nimm die andere Rechnung“) oder Fragen („was machst du gerade?“).
4. Oben auf dem Bildschirm sehen Sie die Leiste:
   - **Pause**: Er hält vor dem nächsten Schritt an, der Rahmen wird gelb. **Weiter** macht weiter.
   - **Stopp**: Er bricht sofort ab und macht keinen Schritt mehr.
5. Notfall: **Stopp** drücken.

## Kosten

- Abgerechnet wird über Ihr KI-Guthaben in hausverwalter.ai, als Posten **„Computer-Bedienung (Claude Sonnet über AWS Bedrock, Frankfurt)“**.
- Im Bedienfenster sehen Sie eine Schätzung (Tokens und ungefähre Kosten). Maßgeblich ist die Abrechnung in hausverwalter.ai.
- Ist das Guthaben leer, meldet er „KI-Guthaben aufgebraucht – in hausverwalter.ai aufladen.“ und hält an.

## Für KI-Agenten (MCP)

Der Assistent ist auch ein MCP-Server namens `computer`. So kann Ihr KI-Agent ihm Aufträge geben, während Sie ganz normal mit Ihrem Agenten sprechen. Werkzeuge:

| Werkzeug | Zweck |
| --- | --- |
| `computer_auftrag` | Auftrag geben (läuft schon einer: Nachricht an ihn) |
| `computer_nachricht` | Korrektur, Antwort oder Frage des Nutzers weitergeben |
| `computer_status` | Bis zu 50 Sekunden auf Neues warten: Zustand, neue Schritte, letzte Nachricht |
| `computer_pause`, `computer_weiter`, `computer_stopp` | Anhalten, weitermachen, abbrechen |

Ablauf für den Agenten: `computer_auftrag` aufrufen, dann `computer_status`, bis er fertig ist oder etwas fragt. Fragen des Assistenten an den Nutzer weitergeben und die Antwort mit `computer_nachricht` zurückschicken. Der Assistent startet von selbst und fragt von selbst nach den Freigaben.

Der Installer trägt OpenCode, die Claude-App und Claude Code selbst ein. Aufbau:

**OpenCode** (`~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "servers": {
      "computer": {
        "type": "local",
        "command": ["<Installationsordner>/venv/bin/python", "<Installationsordner>/app/desktop_agent/mcp_server.py"],
        "timeout": {"startup": 90000, "catalog": 90000, "execution": 90000}
      }
    }
  }
}
```

**Hermes** (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  computer:
    command: "<Installationsordner>/venv/bin/python"
    args: ["<Installationsordner>/app/desktop_agent/mcp_server.py"]
    timeout: 90
```

Unter Windows heißt der Python-Pfad `<Installationsordner>\venv\Scripts\python.exe`.

## Schlüssel ändern oder entfernen

- Neuer Schlüssel: Installationsbefehl erneut ausführen, vorher `HV_NEUER_SCHLUESSEL=1` setzen.
  Mac: `curl -fsSL https://raw.githubusercontent.com/Powerprozesse-GmbH/hausverwalter-computer/main/install.sh | HV_NEUER_SCHLUESSEL=1 bash`
  Windows: `$env:HV_NEUER_SCHLUESSEL = "1"; irm https://raw.githubusercontent.com/Powerprozesse-GmbH/hausverwalter-computer/main/install.ps1 | iex`
- Schlüssel löschen: `<Python> <Installationsordner>/app/desktop_agent/api_key.py loeschen`
- Deinstallieren: Schlüssel löschen, dann den Installationsordner und das Startsymbol löschen.

## Für Entwickler

```bash
uv venv --python 3.12 && uv pip install -r requirements.txt
cd desktop_agent && PYTHONPATH=.. ../.venv/bin/python -m unittest test_agent.py test_mcp_server.py test_win_control.py test_server.py test_api_key.py
```

- `desktop_agent/agent.py`: Ablauf (Modellaufrufe, Pause/Stopp, Zusammenfassung langer Verläufe)
- `desktop_agent/mac_control.py`, `win_control.py`: Bildschirm, Maus, Tastatur, Texterkennung
- `desktop_agent/server.py`: Bedienfenster auf 127.0.0.1:8765, `overlay.py` / `overlay_win.py`: Rahmen und Leiste
- `desktop_agent/mcp_server.py`: MCP-Server für KI-Agenten, `api_key.py`: Schlüssel im Schlüsselspeicher

Die Tests laufen ohne Bildschirmzugriff und ohne KI-Aufrufe. Lizenz: MIT.
