@echo off
cd /d "%~dp0"
echo ===================================================
echo   Syncing Rolex (Astra Mini) to GitHub (LAZY-SLAVE)
echo ===================================================
echo.
git status --short
echo.
set commit_msg=
set /p commit_msg="Enter commit message (or press Enter for auto-timestamp): "
if "%commit_msg%"=="" (
    set commit_msg=Update: %date% %time%
)
echo.
echo Adding files...
git add .
echo Committing: "%commit_msg%"...
git commit -m "%commit_msg%"
echo Pushing to GitHub...
git push origin main
echo.
echo ===================================================
echo   Sync complete!
echo ===================================================
timeout /t 5
