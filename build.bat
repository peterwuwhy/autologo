@echo off
chcp 65001 >nul
REM 打包成免安裝版：在這個資料夾產生 AutoLogo.zip。
REM 把 zip 複製到別台 Windows 電腦，解壓縮後雙擊 AutoLogo.exe 即可（不需安裝 Python）。
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  python -m venv --clear .venv || (echo 請先安裝 Python 3：https://www.python.org/downloads/ & pause & exit /b 1)
)
.venv\Scripts\pip install -r requirements.txt pyinstaller || (echo 套件安裝失敗 & pause & exit /b 1)

REM 打包過程都在 %TEMP%：資料夾在 OneDrive 裡時，同步會鎖住暫存檔導致打包失敗
set OUT=%TEMP%\AutoLogo-build
.venv\Scripts\pyinstaller --noconfirm --clean --workpath "%OUT%\work" --distpath "%OUT%\dist" AutoLogo.spec || (echo 打包失敗 & pause & exit /b 1)

REM 使用者資料放在 exe 旁邊，方便備份與修改
xcopy /e /i /y logos "%OUT%\dist\AutoLogo\logos" >nul
if not exist "%OUT%\dist\AutoLogo\fonts" mkdir "%OUT%\dist\AutoLogo\fonts"
for %%f in (config.json sample.xlsx README.md) do if exist %%f copy /y %%f "%OUT%\dist\AutoLogo\" >nul

powershell -NoProfile -Command "Compress-Archive -Force -Path '%OUT%\dist\AutoLogo' -DestinationPath AutoLogo.zip" || (echo 壓縮失敗 & pause & exit /b 1)
echo.
echo 完成：%~dp0AutoLogo.zip
pause
