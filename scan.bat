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

if exist "%VENVPY%" (
    "%VENVPY%" comfy_audit.py --root "%COMFYDIR%" --days 14 %*
) else (
    python comfy_audit.py --root "%COMFYDIR%" --days 14 %*
)
pause
