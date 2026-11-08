# PyInstaller 設定：把程式打包成不需安裝 Python 的 AutoLogo.exe。請用 build.bat 執行。
import glob

# 原始碼一併放進去，診斷報告的「程式檔版本」才查得到
datas = [("templates", "templates"), ("static", "static")] + [(f, ".") for f in glob.glob("*.py")]

a = Analysis(["app.py"], datas=datas)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AutoLogo", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="AutoLogo")
