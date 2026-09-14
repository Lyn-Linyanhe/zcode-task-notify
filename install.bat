@echo off
rem ---------------------------------------------------------------------------
rem  One-click installer for ZCode Task Notify.
rem  Keep this file ASCII-only: cmd.exe parses .bat using the system ANSI code
rem  page, so Chinese text here gets mangled on some machines. All Chinese
rem  guidance lives in install.py (Python handles encoding at runtime) and in
rem  docs\install-help.txt (opened below when Python is missing).
rem ---------------------------------------------------------------------------
chcp 65001 >nul
title ZCode Task Notify - Setup
cd /d "%~dp0"

echo.
echo   ============================================
echo     ZCode Task Notify - Setup
echo   ============================================
echo.

rem Version probe: catches "python not installed", the Windows Store stub, and
rem anything older than 3.10 (exit code 9) in one shot.
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 9)" >nul 2>nul
if errorlevel 9 goto oldpython
if errorlevel 1 goto nopython

python install.py %*
if errorlevel 1 goto failed
goto done

:nopython
echo   [X] Python was not found - this tool needs it to run.
echo.
echo   Opening the Chinese setup guide in Notepad...
start "" notepad "%~dp0docs\install-help.txt"
goto done

:oldpython
echo   [X] Your Python is too old - version 3.10 or newer is required.
echo.
echo   Opening the Chinese setup guide in Notepad...
start "" notepad "%~dp0docs\install-help.txt"
goto done

:failed
echo.
echo   [!] Setup stopped with an error. Copy the message above when asking
echo       for help. Opening the Chinese troubleshooting guide...
start "" notepad "%~dp0docs\install-help.txt"

:done
echo.
echo   Press any key to close this window...
pause >nul
