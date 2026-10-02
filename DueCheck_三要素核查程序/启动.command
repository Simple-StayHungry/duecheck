#!/bin/bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3.10 或更高版本。"
  read -r -p "按回车关闭"
  exit 1
fi
if ! python3 -c 'import sys; assert sys.version_info >= (3,10)' 2>/dev/null; then
  echo "Python 版本过低，需要 3.10 或更高版本。"
  read -r -p "按回车关闭"
  exit 1
fi

VENV="$DIR/.venv"
PY="$VENV/bin/python"

# 不再 source .venv/bin/activate。
# venv 的 activate 脚本会记录创建时的绝对路径，文件夹改名/移动后可能仍指向旧目录，
# 从而让后台 worker 继续尝试调用旧路径下的 python。始终直接调用当前目录里的解释器即可随目录移动。
if [ ! -x "$PY" ]; then
  echo "首次启动，正在建立本机运行环境…"
  python3 -m venv "$VENV"
fi

# 极少数情况下虚拟环境本身损坏；仅在当前解释器无法启动时重建，不因文件夹改名而重复安装。
if ! "$PY" -c 'import sys; assert sys.version_info >= (3,10)' >/dev/null 2>&1; then
  echo "检测到本机运行环境异常，正在自动修复…"
  rm -rf "$VENV"
  python3 -m venv "$VENV"
fi

if ! "$PY" -c 'import fastapi,uvicorn,lxml,PIL,multipart,cv2,numpy' 2>/dev/null; then
  echo "首次启动，正在安装文档处理组件…"
  "$PY" -m pip install --disable-pip-version-check -r requirements.txt || {
    echo '安装失败，请检查网络后重试。'
    read -r
    exit 1
  }
fi

if [ "$(uname)" = "Darwin" ] && ! "$PY" -c 'import Vision,Foundation' 2>/dev/null; then
  echo "正在安装本机图片识别组件（只需一次，图片不会上传）…"
  "$PY" -m pip install --disable-pip-version-check pyobjc-framework-Vision || \
    echo "图片识别组件安装失败：仍可整理 Word，但图中公司和结果需人工核对。"
fi

# 必须用当前目录下的解释器启动。这样 sys.executable 也会指向当前路径，
# scan_worker / pixel_worker / OCR worker 在文件夹改名后不会再调用旧目录。
exec "$PY" start.py
