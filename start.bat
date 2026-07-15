@echo off
chcp 65001 >nul 2>&1
echo === Image Optimizer ===
echo.
start http://127.0.0.1:8090
python -m app %*
if %errorlevel% neq 0 (
  echo.
  echo [ERROR] Run: pip install -r requirements.txt
  pause
)
pause
