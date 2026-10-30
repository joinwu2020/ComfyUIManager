@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM Runs on the SYSTEM python on purpose - the venv is NOT activated.
REM The manager itself only needs the standard library plus tkinter;
REM each action decides for itself whether it needs the venv:
REM   venv   : start ComfyUI, update, install deps, scan, repair, wheels
REM   system : rebuild the venv (cannot rebuild a venv from inside it)
REM Activating here would make "rebuild venv" impossible.
REM
REM Uses python.exe (not pythonw) so the console stays open and startup
REM tracebacks are visible. For a console-free launch use the shortcut
REM made by make_shortcut.bat, which runs the .pyw file.

python comfy_manager.py
if errorlevel 1 pause
