@echo off
chcp 65001 >nul
REM Windows：雙擊執行。第一次會自動安裝所需套件。
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  python -m venv --clear .venv || (echo 請先安裝 Python 3：https://www.python.org/downloads/ ，安裝時勾選 Add Python to PATH & pause & exit /b 1)
  .venv\Scripts\pip install -r requirements.txt || (echo 套件安裝失敗 & pause & exit /b 1)
)
start "" http://localhost:8000
.venv\Scripts\python app.py
pause
