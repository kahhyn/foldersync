@echo off
rem Install a scheduled task that runs foldersync in the background at logon.
cd /d %~dp0
set PYW=
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set PYW=%%i
if not defined PYW (
  echo [ERROR] pythonw.exe not found. Install Python with "Add to PATH" first.
  pause
  exit /b 1
)
schtasks /Create /F /TN "foldersync" /TR "\"%PYW%\" \"%~dp0foldersync.py\" run" /SC ONLOGON /RL LIMITED
if %errorlevel%==0 (
  echo [OK] Autostart task "foldersync" installed. It will run at next logon.
) else (
  echo [ERROR] Failed to create scheduled task.
)
pause
