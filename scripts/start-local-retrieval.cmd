@echo off
setlocal
set "PYTHONUTF8=1"
set "HF_ENDPOINT=https://hf-mirror.com"
set "HF_HUB_DISABLE_XET=1"
cd /d "%~dp0\.."
".venv\Scripts\python.exe" -m scripts.retrieval_server --port 18081 --cache-dir ".cache\retrieval-models"
