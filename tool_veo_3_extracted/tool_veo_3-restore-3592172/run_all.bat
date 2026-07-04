@echo off
title VietAuto API Server
echo ========================================
echo   VietAuto API - nathamedia.net
echo ========================================
echo.

REM Cài waitress nếu chưa có
pip install waitress --quiet
REM Fix encoding VPS → tránh OSError trên print/logging
set PYTHONIOENCODING=utf-8:replace
chcp 65001 >nul 2>&1

echo [1/2] Khoi dong API server (port 8080)...
start "VietAuto API" cmd /k "cd /d D:\tool_veo_3 && set PYTHONIOENCODING=utf-8:replace && python start_server.py --port 8080"

REM Chờ API khởi động
timeout /t 3 /nobreak > nul

echo [2/2] Khoi dong Caddy (SSL + Public URL)...
start "Caddy Proxy" cmd /k "cd /d D:\tool_veo_3 && caddy.exe run"

echo.
echo ========================================
echo   API dang chay tai:
echo   https://api.nathamedia.net
echo ========================================
echo.
echo Luu y: Phai tro DNS truoc khi Caddy cap SSL!
echo   A Record: api -> IP_VPS_CUA_BAN
echo.
pause
