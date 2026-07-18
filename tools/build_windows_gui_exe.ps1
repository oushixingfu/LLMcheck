param(
    [string]$Version = "0.1.1",
    [string]$PythonLauncher = "py -3"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Venv = Join-Path $Root "tmp\gui-exe-build-venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$ExeName = "LLMcheck-GUI-$Version"

Set-Location $Root

if (-not (Test-Path $Python)) {
    Invoke-Expression "$PythonLauncher -m venv `"$Venv`""
}

& $Python -m pip install --upgrade pip pyinstaller .

& $Python -m PyInstaller `
    --clean `
    --noconfirm `
    --onefile `
    --windowed `
    --name $ExeName `
    --distpath "dist" `
    --workpath "tmp\pyinstaller-build" `
    --specpath "tmp\pyinstaller-spec" `
    --hidden-import tkinter `
    --hidden-import tkinter.ttk `
    --hidden-import tkinter.filedialog `
    --hidden-import tkinter.messagebox `
    "llmcheck\gui_exe.py"

Write-Output (Join-Path $Root "dist\$ExeName.exe")
