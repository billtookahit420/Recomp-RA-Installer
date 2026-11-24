@echo off
rem Recomp RA Installer launcher (Windows): bootstraps a local venv
rem with PySide6; checks the import, not just the venv, so a half-made
rem venv heals itself.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv
)
.venv\Scripts\python -c "import PySide6" 2>nul
if errorlevel 1 .venv\Scripts\pip install --quiet PySide6
.venv\Scripts\python app.py %*
