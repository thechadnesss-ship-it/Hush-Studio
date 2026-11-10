@echo off
rem Double-click to start Hush Studio (no console window stays open).
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" main.py
