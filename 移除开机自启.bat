@echo off
rem Remove the foldersync autostart scheduled task.
schtasks /Delete /TN "foldersync" /F
if %errorlevel%==0 (
  echo [OK] Autostart task removed.
) else (
  echo [INFO] Task not found, nothing to remove.
)
pause
