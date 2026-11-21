@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请先安装 Python 3.11 或更高版本。
  pause
  exit /b 1
)

python -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo [1/3] 安装后端依赖...
  python -m pip install -r "backend\requirements.txt"
) else (
  echo [1/3] 后端依赖已安装。
)

if not exist "frontend\static\main.js" (
  echo [2/3] 构建前端...
  where npm >nul 2>nul
  if errorlevel 1 (
    echo [错误] 未找到 npm，请先安装 Node.js 20 或更高版本。
    pause
    exit /b 1
  )
  pushd frontend
  if not exist "node_modules" call npm install
  call npm run build
  popd
) else (
  echo [2/3] 前端已构建，跳过构建。
)

echo [3/3] 启动 CampusPilot：http://127.0.0.1:8000/
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000/'"
cd /d "%ROOT%backend"
python run.py

endlocal