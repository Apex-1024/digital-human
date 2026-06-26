@echo off
chcp 65001 >nul
setlocal

echo ========================================
echo   课灵 AI 批量制课系统 · 启动脚本
echo ========================================
echo.

REM 检查 Python
where python >nul 2>&1
if errorlevel 1 (
  echo [错误] 未检测到 Python，请先安装 Python 3.8+
  pause
  exit /b 1
)

REM 检查 ffmpeg
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo [警告] 未检测到 ffmpeg，视频合成将无法工作
  echo         请从 https://ffmpeg.org 下载并加入 PATH
  echo.
)

REM 创建虚拟环境（仅当系统支持 venv 时）
if not exist "venv\Scripts\python.exe" (
  python -m venv venv 2>nul
  if errorlevel 1 (
    echo [提示] 当前 Python 不支持 vvenv，将直接使用系统环境
    set "VENV_ACTIVATED=1"
    goto :install
  )
)

echo [1/4] 激活虚拟环境 ...
call venv\Scripts\activate.bat
set "VENV_ACTIVATED=1"

:install
echo [2/4] 安装依赖（首次需要几分钟）...
python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple -q
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple -q
if errorlevel 1 (
  echo [警告] 部分依赖安装失败，尝试使用默认源重试
  pip install -r requirements.txt -q
)

REM 启动服务
echo.
echo [3/4] 启动服务 ...
echo.
echo  ┌──────────────────────────────────────┐
echo  │  工作台地址: http://localhost:7860   │
echo  │  按 Ctrl+C 停止服务                  │
echo  └──────────────────────────────────────┘
echo.

cd backend
python app.py

pause
