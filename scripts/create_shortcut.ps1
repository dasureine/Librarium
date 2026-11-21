# Librarium desktop shortcut creator
$ErrorActionPreference = "Stop"

# 1. Locate pythonw.exe (from the python executable's directory)
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $python) { Write-Error "python not found in PATH" }

$pyDir = Split-Path -Parent $python.Source
$pythonw = Join-Path $pyDir "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) {
    $pythonw = @(
        (Join-Path $pyDir "pythonw.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\pythonw.exe"),
        "C:\Python312\pythonw.exe"
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) {
    Write-Error "pythonw.exe not found"
}
$pythonw = (Resolve-Path -LiteralPath $pythonw).Path

# 2. Project root (parent of scripts)
$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root "scripts\launcher.pyw"
$icon = Join-Path $root "assets\librarium.ico"

# 3. Desktop path
$desktop = [Environment]::GetFolderPath('Desktop')

# 4. Create shortcut
$lnkPath = Join-Path $desktop "Librarium.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnkPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $launcher + '"'
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = $icon
$shortcut.Description = "Librarium local workbench"
$shortcut.Save()

Write-Output ("Shortcut created: " + $lnkPath)
Write-Output ("  target: " + $pythonw + ' "' + $launcher + '"')
Write-Output ("  working dir: " + $root)
Write-Output ("  icon: " + $icon)
