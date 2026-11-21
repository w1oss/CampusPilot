#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if ! python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "[1/3] 安装后端依赖..."
  python -m pip install -r backend/requirements.txt
else
  echo "[1/3] 后端依赖已安装。"
fi

if [ ! -f frontend/static/main.js ]; then
  echo "[2/3] 构建前端..."
  cd frontend
  [ -d node_modules ] || npm install
  npm run build
  cd "$ROOT"
else
  echo "[2/3] 前端已构建，跳过构建。"
fi

echo "[3/3] 启动 CampusPilot：http://127.0.0.1:8000/"
cd backend
exec python run.py