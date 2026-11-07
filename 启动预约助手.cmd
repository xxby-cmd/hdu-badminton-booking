@echo off
setlocal
cd /d "%~dp0"
if exist ".runtime\python.exe" (
  ".runtime\python.exe" -E -s -B startup.py %*
  if errorlevel 1 goto failed
  exit /b 0
)
py -3 -c "import sys,tkinter; assert sys.version_info >= (3,11)" >nul 2>&1
if not errorlevel 1 (
  py -3 -E -s -B startup.py %*
  if errorlevel 1 goto failed
  exit /b 0
)
python -c "import sys,tkinter; assert sys.version_info >= (3,11)" >nul 2>&1
if not errorlevel 1 (
  python -E -s -B startup.py %*
  if errorlevel 1 goto failed
  exit /b 0
)
echo Please use the complete portable ZIP with the .runtime folder.
:failed
echo Startup failed. Keep all extracted files and try again.
pause
exit /b 1
