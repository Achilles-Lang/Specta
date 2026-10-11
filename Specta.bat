@echo off
rem Specta launcher - double-click to start the Daemon with GUI (no console window).
rem Entry point: .venv\Scripts\pythonw.exe -m specta.daemon_main
rem If nothing appears, run ".venv\Scripts\specta.exe" in a terminal to see errors.
setlocal
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m specta.daemon_main
endlocal
