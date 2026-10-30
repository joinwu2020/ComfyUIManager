@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Builds ComfyManager.exe in a SEPARATE venv so PyInstaller never
REM touches the ComfyUI environment.
REM   set COMFY_ONEDIR=1     folder build (lower AV false-positive rate)
REM   set COMFY_NOCONSOLE=1  hide the console
REM   set COMFY_ICON=x.ico   pick an icon explicitly

if not exist ".buildvenv\Scripts\python.exe" (
    echo [INFO] Creating build venv...
    python -m venv .buildvenv || goto :fail
)
call .buildvenv\Scripts\activate.bat
python -m pip install --upgrade pip -q
python -m pip install -q pyinstaller || goto :fail

echo [INFO] Building...
pyinstaller --clean --noconfirm comfy_manager.spec || goto :fail

if exist "dist\ComfyManager.exe" (
    copy /y "dist\ComfyManager.exe" "ComfyManager.exe" >nul
    echo [OK] ComfyManager.exe is in this folder.
) else (
    echo [OK] Folder build is in dist\ComfyManager\
    echo      Copy the WHOLE folder - the exe alone will not run.
)
echo.
pause
exit /b 0

:fail
echo [ERROR] Build failed.
pause
exit /b 1
