@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM Resolve ComfyUI root + venv from comfy_env.py (COMFY_ROOT setting).
REM comfy_env.py only needs the standard library, so the system python can
REM answer this before any venv exists.
set "COMFYDIR="
set "VENVPY="
for /f "usebackq delims=" %%i in (`python -c "import comfy_env,sys;sys.stdout.write(comfy_env.ROOT)" 2^>nul`) do set "COMFYDIR=%%i"
if not defined COMFYDIR set "COMFYDIR=%~dp0"
if "%COMFYDIR:~-1%"=="\" set "COMFYDIR=%COMFYDIR:~0,-1%"
set "VENVPY=%COMFYDIR%\venv\Scripts\python.exe"

if not exist "%VENVPY%" (
    echo [ERROR] venv not found: %VENVPY%
    echo         Run install.bat first, or check COMFY_ROOT in comfy_env.py.
    pause
    exit /b 1
)

REM Stay in the script folder: update.py reads comfy_env.py from here and
REM does its git work inside COMFY_ROOT by itself.
"%VENVPY%" update.py %*
pause
