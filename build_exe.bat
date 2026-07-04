@echo off
setlocal
cd /d "%~dp0"

if not exist .venv (
  py -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

if exist dist\hidemium_controller.exe del /f /q dist\hidemium_controller.exe
if exist dist\hidemiumctl.exe del /f /q dist\hidemiumctl.exe
if exist dist\hidemium_gui.exe del /f /q dist\hidemium_gui.exe
if exist dist\hidemium_gui_silent.exe del /f /q dist\hidemium_gui_silent.exe

pyinstaller --onefile --console --name hidemium_controller gui.py

echo.
echo Build xong 1 file EXE: %CD%\dist\hidemium_controller.exe
echo File nay co giao dien + console log + ghi log vao dist\logs\
echo Chay: dist\hidemium_controller.exe
pause
