#!/usr/bin/env bash
# 课灵 AI 批量制课系统 · 启动脚本 (Linux/macOS)
set -e
echo "========================================"
echo "  课灵 AI 批量制课系统 · 启动脚本"
echo "========================================"

# 检查依赖
command -v python3 >/dev/null 2>&1 || { echo "[错误] 未检测到 python3"; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || echo "[警告] 未检测到 ffmpeg"

# 虚拟环境
if [ ! -d "venv" ]; then
  echo "[1/4] 创建虚拟环境 ..."
  python3 -m venv venv
fi

echo "[2/4] 激活虚拟环境 ..."
source venv/bin/activate

echo "[3/4] 安装依赖 ..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "[4/4] 启动服务 ..."
cd backend
python app.py
