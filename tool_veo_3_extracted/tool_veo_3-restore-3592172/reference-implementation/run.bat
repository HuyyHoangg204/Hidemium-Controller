@echo off
setlocal

cd /d "%~dp0"

set "PY_CMD="

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY_CMD=py -3"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PY_CMD=python"
    )
)

if not defined PY_CMD (
    echo Python was not found in PATH. Install Python 3.11+ and try again.
    exit /b 1
)

echo Using Python command: %PY_CMD%
call %PY_CMD% -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install Python requirements.
    exit /b 1
)

call %PY_CMD% -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo Python is installed, but tkinter is missing.
    echo Install a standard Python build that includes Tk support, then run this file again.
    exit /b 1
)

echo Dependencies installed successfully.
echo Starting app...
call %PY_CMD% gui_app.py
endlocal
