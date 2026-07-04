@echo off
chcp 65001 >nul
echo ============================================
echo   Veo3 Builder
echo ============================================
echo.
echo   [1] Build CO auth (can nhap License Key)
echo   [2] Build KHONG auth (mo la dung luon)
echo.
set /p choice="Chon (1 hoac 2): "

cd /d "%~dp0"

echo.
echo [1/4] Cleaning old build...
if exist _build rmdir /s /q _build 2>nul
if exist dist rmdir /s /q dist 2>nul

echo [2/4] Loading .env...
:: Doc .env file
set "SUPABASE_URL="
set "SUPABASE_KEY="
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if "%%A"=="SUPABASE_URL" set "SUPABASE_URL=%%B"
        if "%%A"=="SUPABASE_KEY" set "SUPABASE_KEY=%%B"
    )
)

if "%SUPABASE_URL%"=="" (
    echo    [ERROR] Khong tim thay SUPABASE_URL trong .env!
    pause
    exit /b 1
)
echo    OK: SUPABASE_URL=%SUPABASE_URL%

echo [3/4] Injecting config into code...
:: Backup app.py va auth.py
copy app.py app.py.bak >nul
copy core\auth.py core\auth.py.bak >nul

:: Inject auth mode
if "%choice%"=="2" (
    echo    -^> Build KHONG AUTH
    powershell -Command "(Get-Content app.py) -replace 'AUTH_ENABLED = True', 'AUTH_ENABLED = False' | Set-Content app.py"
) else (
    echo    -^> Build CO AUTH
)

:: Inject Supabase config vao auth.py (thay placeholder bang gia tri that)
powershell -Command "$c = Get-Content core\auth.py -Raw; $c = $c -replace '__SUPABASE_URL__', '%SUPABASE_URL%'; $c = $c -replace '__SUPABASE_KEY__', '%SUPABASE_KEY%'; Set-Content core\auth.py -Value $c"
echo    OK: Supabase config injected (an trong exe)

echo [4/4] Building Veo3.exe...
pyinstaller app.spec --noconfirm --workpath=_build --distpath=dist

:: Restore original files
move /y app.py.bak app.py >nul
move /y core\auth.py.bak core\auth.py >nul

echo.
if exist "dist\Veo3.exe" (
    echo ============================================
    echo   BUILD THANH CONG!
    echo   File: dist\Veo3.exe
    echo ============================================
    echo.
    echo   Supabase config AN trong exe.
    echo   .env KHONG duoc copy vao dist.
    echo   Chi can copy Veo3.exe sang may khac.
    echo.
) else (
    echo ============================================
    echo   BUILD THAT BAI! Xem log phia tren.
    echo ============================================
)

pause
