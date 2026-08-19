@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Please install Python 3.10+.
  pause
  exit /b 1
)

python -c "import webview, pystray, PIL" >nul 2>nul
if errorlevel 1 (
  echo [FIRST RUN] Installing dependencies...
  python -m pip install pywebview pystray Pillow
  if errorlevel 1 (
    echo [ERROR] Dependency install failed. Check network and retry.
    pause
    exit /b 1
  )
)

start "" pythonw -m core.main
echo Started. Right-click the tray icon to exit.
timeout /t 3 /nobreak >nul
exit /b 0
