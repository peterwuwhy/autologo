"""從 GitHub 檢查並安裝新版。

exe 版下載 Release 裡的 AutoLogo.zip；原始碼版（start.bat 啟動）下載該版本的原始碼。
解壓縮後交給更新批次檔：關閉程式 → 覆蓋程式檔（保留設定、Logo、紀錄）→ 重新啟動。
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile

import settings
from version import GITHUB_REPO, VERSION

FROZEN = getattr(sys, "frozen", False)
ASSET_NAME = "AutoLogo.zip"
_install_lock = threading.Lock()


class UpdateError(Exception):
    pass


def parse_version(s):
    """'v1.2.3' → (1, 2, 3)；沒有數字（例如 'dev'）→ ()，視為最舊。"""
    return tuple(int(n) for n in re.findall(r"\d+", str(s))[:3])


def _open(url, timeout, api=False):
    headers = {"User-Agent": "AutoLogo-updater"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def _latest_release():
    if not GITHUB_REPO:
        raise UpdateError("尚未設定 GitHub 儲存庫（version.py 的 GITHUB_REPO）")
    try:
        with _open(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", 15, api=True) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("GitHub 上還沒有發布任何版本") from None
        raise UpdateError(f"檢查更新失敗：GitHub 回應 {e.code}") from None
    except (OSError, ValueError) as e:
        raise UpdateError(f"無法連線 GitHub（{e}）。請確認這台電腦能上網。") from None


def _asset_url(rel):
    for a in rel.get("assets") or []:
        if a.get("name") == ASSET_NAME:
            return a.get("browser_download_url")
    return None


def check():
    """回傳目前版本、最新版本、是否可以更新、更新內容。"""
    rel = _latest_release()
    latest = (rel.get("tag_name") or "").lstrip("vV")
    newer = parse_version(latest) > parse_version(VERSION)
    info = {
        "current": VERSION,
        "latest": latest,
        "newer": newer,
        "can_install": newer,
        "notes": (rel.get("body") or "").strip(),
        "published": (rel.get("published_at") or "")[:10],
        "message": f"有新版本 {latest}" if newer else f"已經是最新版本（{VERSION}）",
    }
    if newer and FROZEN and not _asset_url(rel):
        info["can_install"] = False
        info["message"] = f"新版本 {latest} 還在打包中，請過幾分鐘再試"
    elif newer and os.name != "nt":
        info["can_install"] = False
        info["message"] = f"有新版本 {latest}，但自動更新只支援 Windows"
    return info


def install():
    """下載並解壓新版，啟動更新批次檔，回傳新版本號。幾秒後程式會被關閉並自動重新啟動。"""
    if os.name != "nt":
        raise UpdateError("自動更新只支援 Windows")
    if not _install_lock.acquire(blocking=False):
        raise UpdateError("已經在更新中，請稍候")
    try:
        rel = _latest_release()
        latest = (rel.get("tag_name") or "").lstrip("vV")
        if parse_version(latest) <= parse_version(VERSION):
            raise UpdateError(f"已經是最新版本（{VERSION}）")
        url = _asset_url(rel) if FROZEN else rel.get("zipball_url")
        if not url:
            raise UpdateError(f"新版本 {latest} 還在打包中，請過幾分鐘再試")

        work = tempfile.mkdtemp(prefix="AutoLogo-update-")
        zip_path = os.path.join(work, "update.zip")
        try:
            with _open(url, 120) as r, open(zip_path, "wb") as f:
                shutil.copyfileobj(r, f)
            new_dir = os.path.join(work, "new")
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(new_dir)
        except (OSError, zipfile.BadZipFile) as e:
            shutil.rmtree(work, ignore_errors=True)
            raise UpdateError(f"下載新版失敗：{e}") from None
        src = _find_root(new_dir, "AutoLogo.exe" if FROZEN else "app.py")
        if not src:
            shutil.rmtree(work, ignore_errors=True)
            raise UpdateError("下載的檔案不完整，請稍後再試")

        bat = os.path.join(work, "update.bat")
        with open(bat, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(UPDATE_BAT)
        # 路徑用環境變數傳，避免批次檔裡的中文路徑編碼問題
        env = dict(
            os.environ,
            AL_SRC=src,
            AL_DST=settings.BASE_DIR,
            AL_WORK=work,
            AL_MODE="exe" if FROZEN else "source",
            AL_PIDS=" ".join(str(p) for p in _pids_to_close()),
        )
        subprocess.Popen(
            ["cmd", "/c", bat], env=env, cwd=work,
            creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True,
        )
        return latest  # 成功就不釋放鎖：程式即將被關閉
    except BaseException:
        _install_lock.release()
        raise


def _find_root(folder, marker):
    """解壓後的程式資料夾（含 marker 檔案的那一層）。"""
    for root, _dirs, files in os.walk(folder):
        if marker in files:
            return root
    return None


def _process_table():
    """Windows 所有行程：{pid: (父行程 pid, 執行檔名稱)}。"""
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    table = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return table


def _pids_to_close():
    """要關掉的行程：程式本身；原始碼版再加上 venv 的 python 啟動器和 start.bat 的黑色視窗。"""
    pids = [os.getpid()]
    if FROZEN:
        return pids
    try:
        table = _process_table()
    except Exception:
        return pids
    pid = os.getpid()
    while True:
        parent = table.get(pid, (0, ""))[0]
        name = table.get(parent, (0, ""))[1]
        if name in ("python.exe", "pythonw.exe", "py.exe"):
            pids.append(parent)
            pid = parent
            continue
        if name == "cmd.exe":
            pids.append(parent)
        return pids


UPDATE_BAT = r"""@echo off
chcp 65001 >nul
title AutoLogo 更新
echo 正在更新 AutoLogo，請不要關閉這個視窗...
timeout /t 2 /nobreak >nul
for %%p in (%AL_PIDS%) do taskkill /F /PID %%p >nul 2>&1
timeout /t 2 /nobreak >nul
set OPTS=/R:10 /W:1 /NFL /NDL /NJH /NJS /NP
if "%AL_MODE%"=="exe" (
  robocopy "%AL_SRC%\_internal" "%AL_DST%\_internal" /MIR %OPTS% >nul
  if errorlevel 8 goto fail
  robocopy "%AL_SRC%" "%AL_DST%" /XF config.json print_log.csv %OPTS% >nul
  if errorlevel 8 goto fail
) else (
  robocopy "%AL_SRC%" "%AL_DST%" /E /XF config.json print_log.csv .gitignore /XD logos output fonts .venv .github %OPTS% >nul
  if errorlevel 8 goto fail
)
rem Logo 只補上新的，不覆蓋現有的
if exist "%AL_SRC%\logos" robocopy "%AL_SRC%\logos" "%AL_DST%\logos" /XC /XN /XO %OPTS% >nul
if "%AL_MODE%"=="source" if exist "%AL_DST%\.venv\Scripts\python.exe" (
  echo 檢查所需套件...
  "%AL_DST%\.venv\Scripts\python.exe" -m pip install -q -r "%AL_DST%\requirements.txt" || echo 套件安裝失敗，程式仍會嘗試啟動
)
echo 更新完成，重新啟動中...
goto restart

:fail
echo.
echo 更新失敗：有檔案無法覆蓋（可能被其他程式開著）。
echo 請關閉所有 AutoLogo 視窗後，重新開啟程式再按一次更新。
pause

:restart
cd /d "%AL_DST%"
if "%AL_MODE%"=="exe" (start "" "%AL_DST%\AutoLogo.exe") else (start "" "%AL_DST%\start.bat")
(goto) 2>nul & rmdir /s /q "%AL_WORK%"
"""
