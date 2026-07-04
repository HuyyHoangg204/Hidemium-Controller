@echo off
setlocal

cd /d "%~dp0"

echo ========================================
echo  Build GUI app to EXE
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python and add it to PATH.
    pause
    exit /b 1
)

echo [1/4] Installing/updating build dependencies...
python -m pip install --upgrade pip pyinstaller -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo [2/4] Installing Playwright Chromium dependencies if needed...
python -m playwright install chromium
if errorlevel 1 (
    echo [WARN] Playwright install failed. The app may still work if browsers are already installed.
)

echo.
echo [3/4] Cleaning previous build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist gui_app.spec del /f /q gui_app.spec

echo.
echo [4/4] Building EXE...
python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name VeoTool ^
    --add-data "browser_scripts.py;." ^
    --hidden-import PySide6.QtCore ^
    --hidden-import PySide6.QtGui ^
    --hidden-import PySide6.QtWidgets ^
    gui_app.py

if errorlevel 1 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Build completed successfully
echo  EXE: %cd%\dist\VeoTool.exe
echo ========================================
pause
