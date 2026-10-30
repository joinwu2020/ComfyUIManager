@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM Runs with the SYSTEM python on purpose: install.py creates the venv
REM and re-executes itself inside it. You cannot rebuild a venv from
REM within that same venv.
REM Where ComfyUI lives is decided by COMFY_ROOT in comfy_env.py.
REM Options pass straight through: install.bat --recreate / --lock
REM                                 --skip-nodes / --overwrite-core

python install.py %*
pause
