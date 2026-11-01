# hausverwalter Computer – Installation unter Windows, ohne Administratorrechte:
#   irm https://raw.githubusercontent.com/Powerprozesse-GmbH/hausverwalter-computer/main/install.ps1 | iex
# Legt alles unter %LOCALAPPDATA%\hausverwalter-computer ab (Python 3.12 über uv), fragt den
# hausverwalter.ai-API-Schlüssel in einem Fenster mit verdeckter Eingabe ab und speichert ihn in der
# Windows-Anmeldeinformationsverwaltung. Erneut ausführen = aktualisieren; der Schlüssel bleibt
# (neu eingeben: vorher $env:HV_NEUER_SCHLUESSEL = "1").
# Runs inside a script block, so nothing leaks into the caller's PowerShell session; errors end it with "throw", never "exit".

& {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is many times faster without the progress bar
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $Repo = "https://github.com/Powerprozesse-GmbH/hausverwalter-computer"
    $Branch = if ($env:HV_BRANCH) { $env:HV_BRANCH } else { "main" }
    $Base = Join-Path $env:LOCALAPPDATA "hausverwalter-computer"
    $App = Join-Path $Base "app"
    $Venv = Join-Path $Base "venv"
    $Py = Join-Path $Venv "Scripts\python.exe"
    $ApiKey = Join-Path $App "desktop_agent\api_key.py"
    $Shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "hausverwalter Computer.lnk"

    function Say([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }

    function Read-HiddenKey {
        # own small window: the key never shows up in a console, a log or the AI agent that runs this script
        Add-Type -AssemblyName System.Windows.Forms
        $form = New-Object System.Windows.Forms.Form
        $form.Text = "hausverwalter Computer"
        $form.ClientSize = New-Object System.Drawing.Size(500, 140)
        $form.StartPosition = "CenterScreen"
        $form.TopMost = $true
        $form.FormBorderStyle = "FixedDialog"
        $form.MaximizeBox = $false
        $form.MinimizeBox = $false
        $label = New-Object System.Windows.Forms.Label
        $label.Text = "Bitte den API-Schlüssel aus hausverwalter.ai einfügen (Einstellungen › API-Schlüssel & MCP). Er wird nur in der Windows-Anmeldeinformationsverwaltung dieses PCs gespeichert."
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

    Say "1/5 uv einrichten (lädt Python, ohne Administratorrechte)"
    $Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $Uv) {
        $env:UV_NO_MODIFY_PATH = "1"
        $env:INSTALLER_NO_MODIFY_PATH = "1"
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
        $Uv = @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $Uv) { throw "uv konnte nicht installiert werden. Internetverbindung prüfen und erneut ausführen." }
    }

    Say "2/5 Programm laden"
    if (Test-Path $Venv) {
        # python.exe is locked while the assistant runs; check before anything is replaced
        try { Remove-Item $Venv -Recurse -Force }
        catch { throw "Der Assistent läuft noch. Bitte sein Fenster schließen und die Installation erneut starten." }
    }
    $Tmp = Join-Path ([IO.Path]::GetTempPath()) ("hv-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $Tmp | Out-Null
    try {
        $Zip = Join-Path $Tmp "app.zip"
        Invoke-WebRequest "$Repo/archive/refs/heads/$Branch.zip" -OutFile $Zip -UseBasicParsing
        Expand-Archive $Zip -DestinationPath $Tmp -Force
        if (Test-Path $App) { Remove-Item $App -Recurse -Force }
        Move-Item (Get-ChildItem $Tmp -Directory | Select-Object -First 1).FullName $App
    } finally {
        Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    Say "3/5 Python 3.12 und Pakete installieren (beim ersten Mal 1–2 Minuten)"
    & $Uv venv --quiet --python 3.12 --python-preference only-managed $Venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 konnte nicht eingerichtet werden." }
    & $Uv pip install --quiet --python $Py -r (Join-Path $App "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Die Pakete konnten nicht installiert werden." }

    Say "4/5 hausverwalter.ai-API-Schlüssel"
    & $Py $ApiKey pruefen | Out-Null
    if ($LASTEXITCODE -eq 0 -and -not $env:HV_NEUER_SCHLUESSEL) {
        Write-Host "Schlüssel ist schon gespeichert."
    } else {
        $key = Read-HiddenKey
        if (-not $key) { throw "Abgebrochen. Zum Einrichten die Installation erneut ausführen." }
        $key | & $Py $ApiKey einrichten --stdin
        $saved = $LASTEXITCODE
        Remove-Variable key
        if ($saved -ne 0) { throw "Der Schlüssel wurde nicht gespeichert." }
    }

    Say "5/5 Startmenü-Eintrag anlegen"
    # The code's default provider is hausverwalter, so the shortcut needs no environment variables.
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Shortcut)
    $link.TargetPath = $Py
    $link.Arguments = '"' + (Join-Path $App "desktop_agent\server.py") + '"'
    $link.WorkingDirectory = Join-Path $App "desktop_agent"
    $link.Description = "hausverwalter Computer: die KI bedient diesen PC"
    $link.Save()

    $McpScript = Join-Path $App "desktop_agent\mcp_server.py"
    $OpenCode = @{ mcp = @{ servers = @{ computer = @{
        type = "local"; command = @($Py, $McpScript); timeout = @{ startup = 90000; catalog = 90000; execution = 90000 }
    } } } } | ConvertTo-Json -Depth 6
    $Yaml = { param([string]$Path) "'" + $Path.Replace("'", "''") + "'" }  # single quotes: backslashes stay as they are
    Write-Host ""
    Write-Host "Fertig. Starten: Startmenü > „hausverwalter Computer“. Oben auf dem Bildschirm erscheint dann eine Leiste mit Pause und Stopp."
    Write-Host ""
    Write-Host "Für KI-Agenten (MCP-Server „computer“), Eintrag in der Konfiguration ergänzen:"
    Write-Host ""
    Write-Host "OpenCode (%USERPROFILE%\.config\opencode\opencode.json):"
    Write-Host $OpenCode
    Write-Host ""
    Write-Host "Hermes (%USERPROFILE%\.hermes\config.yaml):"
    Write-Host "mcp_servers:"
    Write-Host "  computer:"
    Write-Host ("    command: " + (& $Yaml $Py))
    Write-Host ("    args: [" + (& $Yaml $McpScript) + "]")
    Write-Host "    timeout: 90"
}
