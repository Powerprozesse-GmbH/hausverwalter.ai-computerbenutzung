# hausverwalter.ai Computerbenutzung - Installation unter Windows, ohne Administratorrechte:
#   irm https://raw.githubusercontent.com/Powerprozesse-GmbH/hausverwalter.ai-computerbenutzung/main/install.ps1 | iex
# Legt alles unter %LOCALAPPDATA%\hausverwalter-computer ab (Python 3.12 ueber uv), fragt den
# hausverwalter.ai-API-Schluessel in einem Fenster mit verdeckter Eingabe ab und speichert ihn in der
# Windows-Anmeldeinformationsverwaltung. Erneut ausfuehren = aktualisieren; der Schluessel bleibt
# (neu eingeben: vorher $env:HV_NEUER_SCHLUESSEL = "1").
# Runs inside a script block, so nothing leaks into the caller's PowerShell session; errors end it with "throw", never "exit".
# ASCII only (desktop_agent/test_install_ps1.py): PowerShell ends a string at typographic quotes, and Windows PowerShell 5.1
# reads UTF-8 without BOM as ANSI. Umlauts and German quotes come from the char codes below.
# Must also run in ConstrainedLanguage (AppLocker/WDAC script rules): no Add-Type, COM or .NET calls without a fallback.
# Log: %LOCALAPPDATA%\hausverwalter-computer\install.log (ticket 2026-76400).

& {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is many times faster without the progress bar
    $ae = [char]0xE4; $oe = [char]0xF6; $ue = [char]0xFC; $sz = [char]0xDF; $lq = [char]0x201E; $rq = [char]0x201C
    # ConstrainedLanguage blocks this; PowerShell 5.1 on Windows 10/11 already speaks TLS 1.2 there
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
    $Full = $ExecutionContext.SessionState.LanguageMode -eq "FullLanguage"

    $Repo = "https://github.com/Powerprozesse-GmbH/hausverwalter.ai-computerbenutzung"
    $Branch = if ($env:HV_BRANCH) { $env:HV_BRANCH } else { "main" }
    $Base = Join-Path $env:LOCALAPPDATA "hausverwalter-computer"
    $App = Join-Path $Base "app"
    $Venv = Join-Path $Base "venv"
    $Py = Join-Path $Venv "Scripts\python.exe"
    $ApiKey = Join-Path $App "desktop_agent\api_key.py"
    $Log = Join-Path $Base "install.log"
    $Programs = try { [Environment]::GetFolderPath("Programs") } catch { $null }
    if (-not $Programs) { $Programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs" }
    $Shortcut = Join-Path $Programs "hausverwalter.ai Computerbenutzung.lnk"

    function Say([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }

    function Expand-Zip([string]$Zip, [string]$To) {
        # Expand-Archive can fail under a company policy; tar.exe (Windows 10 1803+) unpacks zip too
        try { Expand-Archive $Zip -DestinationPath $To -Force }
        catch {
            New-Item -ItemType Directory -Force -Path $To | Out-Null
            tar -xf $Zip -C $To
            if ($LASTEXITCODE -ne 0) { throw "Entpacken fehlgeschlagen: $Zip" }
        }
    }

    function Read-HiddenKey {
        # own small window: the key never shows up in a console, a log or the AI agent that runs this script
        Add-Type -AssemblyName System.Windows.Forms
        $form = New-Object System.Windows.Forms.Form
        $form.Text = "hausverwalter.ai Computerbenutzung"
        $form.ClientSize = New-Object System.Drawing.Size(500, 140)
        $form.StartPosition = "CenterScreen"
        $form.TopMost = $true
        $form.FormBorderStyle = "FixedDialog"
        $form.MaximizeBox = $false
        $form.MinimizeBox = $false
        $label = New-Object System.Windows.Forms.Label
        $label.Text = "Ich habe die Schl${ue}ssel-Seite von hausverwalter.ai ge${oe}ffnet. Klick dort auf ${lq}Erstellen${rq} und ${lq}Kopieren${rq} und f${ue}ge den Schl${ue}ssel hier ein."
        $label.SetBounds(12, 10, 476, 44)
        $box = New-Object System.Windows.Forms.TextBox
        $box.UseSystemPasswordChar = $true
        $box.SetBounds(12, 62, 476, 24)
        $ok = New-Object System.Windows.Forms.Button
        $ok.Text = "Speichern"
        $ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $ok.SetBounds(306, 100, 88, 28)
        $cancel = New-Object System.Windows.Forms.Button
        $cancel.Text = "Abbrechen"
        $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
        $cancel.SetBounds(400, 100, 88, 28)
        $form.Controls.AddRange(@($label, $box, $ok, $cancel))
        $form.AcceptButton = $ok
        $form.CancelButton = $cancel
        $form.Add_Shown({ $form.Activate(); $box.Focus() })
        $answer = $form.ShowDialog()
        $text = $box.Text
        $form.Dispose()
        if ($answer -ne [System.Windows.Forms.DialogResult]::OK) { return $null }
        return $text
    }

    New-Item -ItemType Directory -Force -Path $Base | Out-Null
    try { Start-Transcript -Path $Log -Force | Out-Null } catch {}
    try {
    if (-not $Full) {
        Write-Host "PowerShell l${ae}uft hier im eingeschr${ae}nkten Modus (Firmenrichtlinie, z. B. AppLocker). Die Installation l${ae}uft trotzdem."
    }

    Say "1/5 uv einrichten (l${ae}dt Python, ohne Administratorrechte)"
    $Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $Uv) {
        $env:UV_NO_MODIFY_PATH = "1"
        $env:INSTALLER_NO_MODIFY_PATH = "1"
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
        $Uv = @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $Uv) {
            # uv's own installer needs full PowerShell; under a company policy load uv.exe directly
            $UvDir = Join-Path $Base "uv"
            $UvZip = Join-Path $env:TEMP ("hv-uv-" + [guid]::NewGuid() + ".zip")
            $Arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "aarch64" } else { "x86_64" }
            try {
                Invoke-WebRequest "https://github.com/astral-sh/uv/releases/latest/download/uv-$Arch-pc-windows-msvc.zip" -OutFile $UvZip -UseBasicParsing
                Expand-Zip $UvZip $UvDir
            } catch {} finally { Remove-Item $UvZip -Force -ErrorAction SilentlyContinue }
            $Uv = Get-ChildItem $UvDir -Recurse -Filter uv.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
        }
        if (-not $Uv) { throw "uv konnte nicht installiert werden. Internetverbindung pr${ue}fen und erneut ausf${ue}hren." }
    }

    # Claude-App keeps its config in memory and writes it back on exit, so an entry written while it runs is lost;
    # it also runs our MCP server from the venv. Close it now, start it again at the end.
    # Never close the Claude this installer runs in (e.g. its Code tab): walk up our own parent processes.
    $Mine = @(); $id = $PID
    while ($id -and $Mine.Count -lt 30) {
        $Mine += $id
        $id = (Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue).ParentProcessId
    }
    $ClaudeApp = @(Get-Process claude -ErrorAction SilentlyContinue | Where-Object { $_.Path -match '\\WindowsApps\\Claude_|\\AnthropicClaude\\' })
    $InsideClaude = @($ClaudeApp | Where-Object { $Mine -contains $_.Id }).Count -gt 0
    $Restart = $ClaudeApp.Count -gt 0 -and -not $InsideClaude
    if ($Restart) {
        Write-Host "Claude-App wird kurz beendet und am Ende wieder gestartet, damit sie ${lq}computer${rq} kennt."
        $ClaudeApp | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    Say "2/5 Programm laden"
    # an update replaces the running program; it has no window the person could close
    Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    # MCP servers that AI programs started from the venv lock it too (they outlive a killed Claude)
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($Base, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and $Mine -notcontains $_.ProcessId } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    if (Test-Path $Venv) {
        # python.exe is locked while the assistant runs; check before anything is replaced
        try { Remove-Item $Venv -Recurse -Force }
        catch { throw "Der Assistent l${ae}uft noch. Bitte sein Fenster schlie${sz}en und die Installation erneut starten." }
    }
    $Tmp = Join-Path $env:TEMP ("hv-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $Tmp | Out-Null
    try {
        $Zip = Join-Path $Tmp "app.zip"
        Invoke-WebRequest "$Repo/archive/refs/heads/$Branch.zip" -OutFile $Zip -UseBasicParsing
        Expand-Zip $Zip $Tmp
        if (Test-Path $App) { Remove-Item $App -Recurse -Force }
        Move-Item (Get-ChildItem $Tmp -Directory | Select-Object -First 1).FullName $App
    } finally {
        Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    Say "3/5 Python 3.12 und Pakete installieren (beim ersten Mal 1-2 Minuten)"
    & $Uv venv --quiet --python 3.12 --python-preference only-managed $Venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 konnte nicht eingerichtet werden." }
    & $Uv pip install --quiet --python $Py -r (Join-Path $App "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Die Pakete konnten nicht installiert werden." }

    Say "4/5 hausverwalter.ai-API-Schl${ue}ssel"
    & $Py $ApiKey pruefen | Out-Null
    if ($LASTEXITCODE -eq 0 -and -not $env:HV_NEUER_SCHLUESSEL) {
        Write-Host "Schl${ue}ssel ist schon gespeichert."
    } elseif ($env:HV_KI) {
        Write-Host "Noch kein Schl${ue}ssel: Die KI fragt im Chat danach."
    } else {
        Start-Process $(if ($env:HV_KEY_URL) { $env:HV_KEY_URL } else { "https://app.hausverwalter.ai/settings/api-keys" })
        Start-Sleep -Seconds 2
        if ($Full) {
            $key = Read-HiddenKey
            if (-not $key) { throw "Abgebrochen. Zum Einrichten die Installation erneut ausf${ue}hren." }
            $key | & $Py $ApiKey einrichten --stdin
            $saved = $LASTEXITCODE
            Remove-Variable key
        } else {
            # no Windows Forms here: Python asks in this window, the input stays hidden
            Write-Host "Klick dort auf ${lq}Erstellen${rq} und ${lq}Kopieren${rq}, dann hier mit Rechtsklick einf${ue}gen und Enter dr${ue}cken (bleibt unsichtbar)."
            & $Py $ApiKey einrichten
            $saved = $LASTEXITCODE
        }
        if ($saved -ne 0) { throw "Der Schl${ue}ssel wurde nicht gespeichert." }
    }

    Say "5/5 Startmen${ue}-Eintrag anlegen"
    # The code's default provider is hausverwalter, so the shortcut needs no environment variables.
    $Pythonw = Join-Path $Venv "Scripts\pythonw.exe"  # no console window
    $Server = '"' + (Join-Path $App "desktop_agent\server.py") + '"'
    $HaveShortcut = $false
    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($Shortcut)
        $link.TargetPath = $Pythonw
        $link.Arguments = $Server
        $link.WorkingDirectory = Join-Path $App "desktop_agent"
        $link.Description = "hausverwalter.ai Computerbenutzung: die KI bedient diesen PC"
        $link.Save()
        $HaveShortcut = $true
    } catch { Write-Host "Startmen${ue}-Eintrag nicht m${oe}glich (Firmenrichtlinie). Die KI startet den Assistenten trotzdem selbst." }

    $McpScript = Join-Path $App "desktop_agent\mcp_server.py"
    # one line per program ("Claude-App: neu"); shown and kept in the log
    $Entries = @(& $Py (Join-Path $App "desktop_agent\mcp_eintragen.py") $Py $McpScript)
    $Entries | ForEach-Object { Write-Host $_ }
    $ClaudeEntry = "$($Entries | Where-Object { $_ -like 'Claude-App:*' } | Select-Object -First 1)"
    $Problems = @()
    if ($ClaudeEntry -match 'kein Programm') { $Problems += "Claude-App nicht gefunden. Bitte Claude-App einmal starten und anmelden, dann die Installation erneut ausf${ue}hren." }
    if ($ClaudeEntry -match 'nicht lesbar') { $Problems += "Die Einstellungsdatei der Claude-App ist besch${ae}digt (claude_desktop_config.json), ${lq}computer${rq} konnte nicht eingetragen werden." }
    # Team/Enterprise admins can switch off local MCP servers; Claude then ignores the entry silently
    $Policy = try { (Get-ItemProperty "HKLM:\SOFTWARE\Policies\Claude" -ErrorAction Stop).isLocalDevMcpEnabled } catch { $null }
    if ($null -ne $Policy -and "$Policy" -match '^(0|false)$') { $Problems += "Deine Firma hat in der Claude-App eigene Konnektoren gesperrt (Richtlinie isLocalDevMcpEnabled). Die IT muss sie freigeben, sonst erscheint ${lq}computer${rq} nicht." }
    if ($Restart) {
        try {
            $StartApp = Get-StartApps | Where-Object Name -eq "Claude" | Select-Object -First 1
            if ($StartApp) { Start-Process ("shell:AppsFolder\" + $StartApp.AppID) } else { Write-Host "Claude-App bitte selbst wieder starten." }
        } catch { Write-Host "Claude-App bitte selbst wieder starten." }
    }
    if ($InsideClaude) { Write-Host "Wichtig: Claude-App einmal ganz beenden (auch im Infobereich unten rechts) und die Installation in PowerShell erneut ausf${ue}hren, sonst fehlt ${lq}computer${rq}." }
    & $Py $ApiKey pruefen | Out-Null
    if ($LASTEXITCODE -eq 0) {
        if ($HaveShortcut) { Start-Process $Shortcut }
        else { Start-Process $Pythonw -ArgumentList $Server -WorkingDirectory (Join-Path $App "desktop_agent") }
    }
    $Yaml = { param([string]$Path) "'" + $Path.Replace("'", "''") + "'" }  # single quotes: backslashes stay as they are
    Write-Host ""
    foreach ($problem in $Problems) { Write-Host ""; Write-Host "ACHTUNG: $problem" -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "Protokoll: $Log"
    if ($HaveShortcut) { Write-Host "Fertig, der Assistent startet jetzt. Sp${ae}ter starten: Startmen${ue} > ${lq}hausverwalter.ai Computerbenutzung${rq}." }
    else { Write-Host "Fertig, der Assistent startet jetzt." }
    Write-Host ""
    Write-Host "KI-Agenten: Das Werkzeug ${lq}computer_status${rq} startet ihn von selbst. OpenCode, Claude-App und Claude Code sind eingetragen, falls vorhanden."
    Write-Host "Hermes (%USERPROFILE%\.hermes\config.yaml) von Hand:"
    Write-Host "mcp_servers:"
    Write-Host "  computer:"
    Write-Host ("    command: " + (& $Yaml $Py))
    Write-Host ("    args: [" + (& $Yaml $McpScript) + "]")
    Write-Host "    timeout: 90"
    } catch {
        Write-Host ""
        Write-Host "Fehler: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Protokoll: $Log"
        throw
    } finally {
        try { Stop-Transcript | Out-Null } catch {}
    }
}
