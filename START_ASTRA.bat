@echo off
setlocal
title Rolex - Voice Screen Agent
color 0A
cd /d "%~dp0"

echo.
echo   ============================================
echo    ROLEX — Voice Screen ^& Desktop Agent
echo   ============================================
echo.

:: Detect working Python interpreter
set "PY_CMD="
py -c "import sys; sys.exit(0)" >nul 2>&1 && set "PY_CMD=py"

if not defined PY_CMD (
    if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" (
        set "PY_CMD=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
    ) else if exist "%LOCALAPPDATA%\Python\bin\python.exe" (
        set "PY_CMD=%LOCALAPPDATA%\Python\bin\python.exe"
    ) else (
        set "PY_CMD=python"
    )
)

echo   [OK] Found Python interpreter.
echo.
echo   Starting Rolex...
echo   (Ctrl+C to quit ^| Move mouse to top-left corner for emergency stop)
echo.
echo   ------------------------------------------------------------
echo   * IMPORTANT: Rolex starts in SLEEP mode to prevent accidents.
echo   * You MUST say "Rolex" to wake it up! (e.g. "Rolex open Chrome")
echo   ------------------------------------------------------------
echo.

"%PY_CMD%" astra_mini.py

echo.
echo   Rolex exited.
pause
