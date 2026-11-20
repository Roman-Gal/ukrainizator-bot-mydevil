$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the Python environment first.' }
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop -or -not (Test-Path -LiteralPath $desktop)) { throw 'Desktop folder not found.' }
$shortcutPath = Join-Path $desktop 'Telegram Bots.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
if ((Test-Path -LiteralPath $shortcutPath) -and $shortcut.TargetPath -ne $python) {
    throw 'An unrelated Telegram Bots shortcut already exists; it was not changed.'
}
$shortcut.TargetPath = $python
$shortcut.Arguments = '-X utf8 -m bot_control --start'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.IconLocation = "$python,0"
$shortcut.WindowStyle = 1
$shortcut.Description = 'Start the Telegram planner, translator, LibreTranslate and ngrok.'
$shortcut.Save()
Write-Host "Desktop shortcut created: $shortcutPath"
