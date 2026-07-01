@echo off
title 课灵学习手册预览服务器 (端口 8899)
cd /d "c:\Users\sun\Desktop\yuantu-test\digital-human\keling-learning-plan"

echo ============================================
echo   课灵 AI 学习手册预览服务器
echo   请勿关闭此窗口！关闭后服务将停止。
echo ============================================
echo.

REM --- 自动查找 python.exe ---
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
    for %%P in (
        "C:\Users\sun\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\vm\tools\python\python.exe"
        "C:\tools\python3.11.9\python.exe"
        "C:\Python311\python.exe"
        "C:\Python310\python.exe"
        "C:\Python39\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    ) do (
        if exist %%P set "PY=%%~P"
    )
)

if not defined PY (
    echo [错误] 未找到 python.exe！
    echo 请安装 Python 或将其加入系统 PATH。
    echo.
    pause
    exit /b 1
)

echo 找到 Python: %PY%
echo.
echo 正在启动服务器...
echo 浏览器请访问: http://localhost:8899/keling-learning-plan.html
echo.

REM 3秒后自动打开浏览器
start "" /b /wait powershell -Command "Start-Sleep -Seconds 3; Start-Process 'http://localhost:8899/keling-learning-plan.html'"

REM 启动服务器（前台运行，关闭窗口即停止）
"%PY%" -m http.server 8899

pause
