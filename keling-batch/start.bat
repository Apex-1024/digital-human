@echo off
chcp 65001 >nul
title 课灵 AI 批量制课系统

echo ============================================
echo  课灵 AI 批量制课系统 - 启动脚本
echo ============================================
echo.

REM 1. 清理占用 7860 端口的旧进程
echo [1/3] 检查 7860 端口占用...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":7860" ^| findstr "LISTENING"') do (
    echo  发现占用进程 PID=%%a，正在停止...
    taskkill /F /PID %%a >nul 2>&1
    timeout /t 1 /nobreak >nul
)

REM 2. 确认 Python 环境
echo.
echo [2/3] 检查 Python 环境...
python --version 2>nul
if errorlevel 1 (
    echo  错误：未找到 python 命令，请检查 PATH 配置
    pause
    exit /b 1
)

REM 3. 启动服务
echo.
echo [3/3] 启动服务（端口 7860）...
echo  浏览器访问：http://localhost:7860/
echo  按 Ctrl+C 停止服务
echo.
python -u backend\app.py

pause
