@echo off
rem Launcher for croco. Put a shortcut to THIS file in the Startup folder
rem (see install_startup.ps1).
rem
rem NOTE: keep this file ASCII-only. cmd.exe re-reads a .bat file using the
rem active codepage, so mixing non-ASCII text with "chcp 65001" shifts byte
rem offsets and corrupts parsing of every line that follows. All Japanese
rem output is produced by Python instead (PYTHONUTF8=1).

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

cd /d "%~dp0.."

python run_croco.py %*
set EXITCODE=%ERRORLEVEL%

echo.
if not "%EXITCODE%"=="0" (
    echo croco exited with code %EXITCODE%. Log: "%~dp0..\logs"
    echo.
)

rem Always keep the window open so the summary above stays readable.
rem The run is already finished at this point; closing the window is safe.
pause

exit /b %EXITCODE%
