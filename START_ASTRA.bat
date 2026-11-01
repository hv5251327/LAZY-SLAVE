@echo off
title Rolex - Voice Screen Agent
color 0A
echo.
echo   ============================================
echo    ROLEX — Voice Screen ^& Desktop Agent
echo   ============================================
echo.
echo   Checking dependencies...
py -m pip install -r "%~dp0requirements.txt" --quiet 2>nul
echo   Done.
echo.
echo   Starting Rolex... (Ctrl+C or say "exit" to quit)
echo   (Move mouse to top-left corner = emergency stop)
echo.
py "%~dp0astra_mini.py"
echo.
echo   Rolex exited.
pause
