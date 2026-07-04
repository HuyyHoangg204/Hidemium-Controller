@echo off
cd /d "%~dp0"

SET "PYTHON=%~dp0.venv\Scripts\python.exe"
IF NOT EXIST "%PYTHON%" SET "PYTHON=python"

SET "NODE=node"
IF EXIST "C:\Program Files\nodejs\node.exe" SET "NODE=C:\Program Files\nodejs\node.exe"

"%PYTHON%" -c "import pymongo" 2>nul
IF ERRORLEVEL 1 (
    echo [ERROR] Python tai %PYTHON% khong co pymongo!
    echo Dang cai pymongo...
    "%PYTHON%" -m pip install pymongo --quiet
)

SET INTERVAL_MINUTES=60

echo ==========================================
echo  Thong bao Cookie Veo3 - Daemon
echo  Python: %PYTHON%
echo  Node  : %NODE%
echo  Lap lai moi: %INTERVAL_MINUTES% phut
echo ==========================================
echo.

"%NODE%" thongbao.js --daemon
pause
