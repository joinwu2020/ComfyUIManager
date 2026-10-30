@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Creates a desktop shortcut that launches comfy_manager.pyw with
REM pythonw.exe - no console window, no flashing child windows.

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

REM Prefer the SYSTEM pythonw so that "rebuild venv" keeps working
REM (install.py --recreate refuses to run from inside the venv).
set "PYW="
for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do (
    if not defined PYW set "PYW=%%i"
)
if not defined PYW (
    if exist "%ROOT%\venv\Scripts\pythonw.exe" (
        set "PYW=%ROOT%\venv\Scripts\pythonw.exe"
        echo [WARN] No system pythonw.exe on PATH; using the venv one.
        echo        "Rebuild venv" will then tell you to use a plain
        echo        command prompt instead.
    )
)
if not defined PYW (
    echo [ERROR] pythonw.exe not found. Install Python 3.12 or run install.bat.
    pause
    exit /b 1
)

set "TARGET=%ROOT%\comfy_manager.pyw"
if not exist "%TARGET%" (
    echo [ERROR] comfy_manager.pyw not found in %ROOT%
    pause
    exit /b 1
)

set "ICON=%ROOT%\comfyui.ico"
if not exist "%ICON%" set "ICON=%PYW%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell);" ^
  "foreach($d in @([Environment]::GetFolderPath('Desktop'), '%ROOT%')){" ^
  "  $l=$s.CreateShortcut((Join-Path $d 'Comfy Manager.lnk'));" ^
  "  $l.TargetPath='%PYW%';" ^
  "  $l.Arguments='\"%TARGET%\"';" ^
  "  $l.WorkingDirectory='%ROOT%';" ^
  "  $l.IconLocation='%ICON%,0';" ^
  "  $l.Description='ComfyUI environment manager';" ^
  "  $l.Save();" ^
  "  Write-Host ('[OK] ' + (Join-Path $d 'Comfy Manager.lnk'))}"

echo.
echo Launcher : %PYW%
echo Script   : %TARGET%
echo Icon     : %ICON%
echo.
pause
