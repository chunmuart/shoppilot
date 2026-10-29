@echo off
setlocal
cd /d "%~dp0"
set "PAUSE=pause"
if /i "%~1"=="--no-pause" set "PAUSE=echo."

echo ============================================
echo  ShopPilot 本地一键启动
echo  检索 18081  /  后端 8000  /  前端 5173
echo ============================================
echo.

if not exist ".env" (
  if not exist ".env.example" (
    echo [错误] 缺少 .env 和 .env.example，无法启动。
    pause
    exit /b 1
  )
  copy ".env.example" ".env" >nul
  echo [警告] 已从 .env.example 生成 .env。
  echo        请先填写 LLM_API_KEY 再重新运行本脚本。
  %PAUSE%
  exit /b 1
)

findstr /r /c:"^LLM_API_KEY=." .env >nul 2>&1
if errorlevel 1 (
  echo [警告] .env 中的 LLM_API_KEY 为空，模型调用会失败。
  echo        请填写后重新运行。
  %PAUSE%
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未找到 .venv，请先执行：
  echo        uv sync --frozen
  %PAUSE%
  exit /b 1
)

if not exist "frontend\node_modules" (
  echo [提示] 未安装前端依赖，正在执行 npm ci ...
  call npm --prefix frontend ci
  if errorlevel 1 (
    echo [错误] 前端依赖安装失败。
    pause
    exit /b 1
  )
)

if not exist ".cache\retrieval-models" (
  echo [提示] 首次运行需要下载检索模型，可能需要几分钟。
)

echo [1/3] 启动本地检索服务 18081 ...
start "ShopPilot 检索 18081" cmd /k "scripts\start-local-retrieval.cmd"

echo [2/3] 启动后端 8000 ...
start "ShopPilot 后端 8000" cmd /k "set PYTHONUTF8=1&& .venv\Scripts\python.exe -m uvicorn app.presentation.server:app --host 127.0.0.1 --port 8000"

echo [3/3] 启动前端 5173 ...
start "ShopPilot 前端 5173" cmd /k "npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173"

echo.
echo 三个服务各占一个窗口，关闭窗口或按 Ctrl-C 即可停止。
echo 接口文档: http://127.0.0.1:8000/docs
echo.
echo 等待后端就绪（最多 120 秒）...
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 60;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8000/health' -TimeoutSec 2; if($r.StatusCode -eq 200){$ok=$true;break} } catch {}; Start-Sleep -Seconds 2 }; if($ok){ Write-Host '[OK] 后端已就绪，正在打开 http://127.0.0.1:5173' -ForegroundColor Green; Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:5173' } else { Write-Host '[警告] 后端 120 秒内未就绪，请查看「ShopPilot 后端 8000」窗口。' -ForegroundColor Yellow }"

%PAUSE%
