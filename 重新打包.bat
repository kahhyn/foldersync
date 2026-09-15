@echo off
rem Rebuild foldersync-gui.exe with PyInstaller.
cd /d %~dp0
python -m PyInstaller --onefile --windowed --name foldersync-gui --clean -y foldersync_gui.py
if %errorlevel%==0 (
  copy /y dist\foldersync-gui.exe . >nul
  echo [OK] foldersync-gui.exe rebuilt.
) else (
  echo [ERROR] Build failed.
)
pause
