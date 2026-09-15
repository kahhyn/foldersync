@echo off
rem Run foldersync in a visible console window (for debugging).
cd /d %~dp0
python foldersync.py run
pause
