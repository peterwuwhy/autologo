"""床墊標籤列印工具 — 網頁伺服器。執行：python app.py，然後開 http://localhost:8000"""
import csv
import datetime
import io
import os
import re
import socket
import sys
import threading
import time
import uuid
import webbrowser
from collections import OrderedDict

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

import diagnostics
import excel_reader
import ezpl
import settings
import updater
import winprint
from label_render import SAMPLE_LABEL, render_png
from make_template import template_bytes
from version import VERSION

PORT = 8000
app = Flask(
    __name__,
    template_folder=os.path.join(settings.RESOURCE_DIR, "templates"),
    static_folder=os.path.join(settings.RESOURCE_DIR, "static"),
)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.json.ensure_ascii = False

# 最近上傳的批次（記憶體內，重啟即清空）
batches = OrderedDict()
MAX_BATCHES = 20

# 印表機是否回應狀態查詢：None = 未知；False = 不回應，之後列印時略過查詢
_status_supported = None
POST_CHECK_SECONDS = 2  # 送出後等幾秒再查一次狀態，抓「送紙中途報錯」
# 網頁伺服器是多執行緒；印表機一次只處理一件事，避免列印、查詢、測試的連線互相干擾
printer_lock = threading.Lock()


class PrinterNotReady(Exception):
    pass


def _get_label(batch_id, index):
    labels = batches.get(batch_id)
    if labels is None or not (0 <= index < len(labels)):
        abort(404, "找不到這批資料，請重新上傳 Excel")
    return labels[index]


def _log(label, result, message):
    new = not os.path.exists(settings.LOG_PATH)
    with open(settings.LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["時間", "型號", "尺寸", "張數", "結果", "訊息"])
        w.writerow([
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            label["型號"], f'{label["寬"]}x{label["長"]}', label["數量"], result, message,
        ])


@app.errorhandler(HTTPException)
def api_error(e):
    """/api/ 的錯誤回傳 JSON，讓網頁能顯示中文訊息。"""
    if request.path.startswith("/api/"):
        return jsonify(ok=False, message=e.description), e.code
    return e


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify(error="沒有收到檔案"), 400
    result = excel_reader.read(io.BytesIO(f.read()))
    batch_id = uuid.uuid4().hex[:12]
    batches[batch_id] = result["labels"]
    while len(batches) > MAX_BATCHES:
        batches.popitem(last=False)
    return jsonify(batch_id=batch_id, **result)


@app.get("/api/preview/<batch_id>/<int:index>.png")
def preview(batch_id, index):
    label = SAMPLE_LABEL if batch_id == "sample" else _get_label(batch_id, index)
    buf = io.BytesIO()
    try:
        render_png(label, settings.load(), buf)
    except Exception as e:
        abort(500, str(e))
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)


def _check_status(cfg):
    """印表機回報錯誤時丟 PrinterNotReady；印表機不回應狀態查詢就略過。"""
    global _status_supported
    if cfg["printer"].get("dry_run") or _status_supported is False:
        return
    st = ezpl.query_status(cfg)
    if st["code"] is None:
        _status_supported = False
        return
    _status_supported = True
    if st["code"] not in ezpl.READY_CODES:
        raise PrinterNotReady(f'印表機回報錯誤 {st["text"]}')


def _send_job(kind, build, label=None, name="label"):
    with printer_lock:
        return _send_job_locked(kind, build, label, name)


def _send_job_locked(kind, build, label, name):
    """build(cfg) 產生指令；送出前後檢查印表機狀態，並記錄到 last_job.json。"""
    cfg = settings.load()
    job = None
    try:
        _check_status(cfg)
        job = build(cfg)
        message = ezpl.send(job, cfg, name=name)
        if _status_supported and not cfg["printer"].get("dry_run"):
            time.sleep(POST_CHECK_SECONDS)
            try:
                _check_status(cfg)
            except PrinterNotReady as e:
                raise PrinterNotReady(f"指令已送出，但{e}") from None
        ok = True
    except PrinterNotReady as e:
        ok, message = False, str(e)
    except (OSError, socket.timeout) as e:
        ok, message = False, f"無法連線印表機 {ezpl.address(cfg)}（{e}）"
    except Exception as e:
        ok, message = False, f"{type(e).__name__}：{e}"
    ezpl.save_last_job(kind, job, ok, message, label)
    return ok, message


def _print(label, copies=None, kind="一般列印"):
    name = re.sub(r'[\\/:*?"<>|\s]', "_", f'{label["型號"]}_{label["寬"]}x{label["長"]}')
    return _send_job(kind, lambda cfg: ezpl.build_job(label, cfg, copies=copies), label, name)


@app.post("/api/print")
def print_label():
    data = request.get_json(force=True)
    label = _get_label(data.get("batch_id"), int(data.get("index", -1)))
    ok, message = _print(label)
    _log(label, "成功" if ok else "失敗", message)
    return jsonify(ok=ok, message=message)


@app.post("/api/test-print")
def test_print():
    ok, message = _print(SAMPLE_LABEL, copies=1, kind="測試列印")
    return jsonify(ok=ok, message=message)


@app.post("/api/basic-test")
def basic_test():
    ok, message = _send_job("基本測試（不含圖片）", ezpl.build_basic_test, name="basic_test")
    return jsonify(ok=ok, message=message)


@app.post("/api/full-test")
def full_test():
    ok, message = _send_job("綜合測試（3 張）", ezpl.build_full_test, name="full_test")
    return jsonify(ok=ok, message=message)


@app.post("/api/printer-command")
def printer_command():
    name = (request.get_json(force=True) or {}).get("name")
    if name not in ezpl.COMMANDS:
        return jsonify(ok=False, message="不支援的指令"), 400
    cfg = settings.load()
    if cfg["printer"].get("dry_run"):
        return jsonify(ok=False, message="目前為測試模式，不會送指令到印表機")
    try:
        with printer_lock:
            ezpl.send_command(cfg, name)
        return jsonify(ok=True, message="已送出")
    except OSError as e:
        return jsonify(ok=False, message=f"連線失敗：{e}")


@app.post("/api/test-connection")
def test_connection():
    cfg = settings.load()
    if cfg["printer"].get("dry_run"):
        return jsonify(ok=True, message="目前為「不送印表機」測試模式")
    try:
        with printer_lock:
            st = ezpl.query_status(cfg)
    except OSError as e:
        return jsonify(ok=False, message=f"連線失敗：{e}")
    ok = st["code"] is None or st["code"] in ezpl.READY_CODES
    return jsonify(ok=ok, message=f'已連上 {ezpl.address(cfg)}。印表機狀態 {st["text"]}')


def _save_changes(changes):
    """套用部分設定並存檔；回傳 (cfg, 錯誤訊息)。"""
    global _status_supported
    cfg = settings._merge(settings.load(), changes)
    errors = settings.validate(cfg)
    if errors:
        return None, "；".join(errors)
    settings.save(cfg)
    _status_supported = None
    return cfg, None


def _number(data, key):
    try:
        return float(data.get(key))
    except (TypeError, ValueError):
        abort(400, "請填入數字")


@app.post("/api/calibrate/media")
def calibrate_media():
    """校正步驟 2：紙張類型與列印方式。存檔後清除錯誤、把設定寫進印表機（不列印），再讀狀態。"""
    data = request.get_json(force=True) or {}
    continuous = data.get("paper") == "continuous"
    gap = 0 if continuous else _number(data, "gap_mm")
    if not continuous and gap <= 0:
        return jsonify(ok=False, message="間距紙的間距要大於 0；整捲沒有分隔請選「連續紙」"), 400
    cfg, err = _save_changes({"label": {"gap_mm": gap}, "printer": {"print_mode": data.get("print_mode")}})
    if err:
        return jsonify(ok=False, message=err), 400
    if cfg["printer"].get("dry_run"):
        return jsonify(ok=True, message="已儲存（測試模式，不會送到印表機）")
    try:
        with printer_lock:
            ezpl.send(ezpl.media_setup(cfg), cfg, name="media")
            time.sleep(1)
            st = ezpl.query_status(cfg)
    except OSError as e:
        return jsonify(ok=False, message=f"已儲存，但無法連線印表機 {ezpl.address(cfg)}（{e}）")
    ok = st["code"] is None or st["code"] in ezpl.READY_CODES
    return jsonify(ok=ok, message=f'已儲存並寫入印表機。印表機狀態 {st["text"]}')


@app.post("/api/calibrate/ruler")
def calibrate_ruler():
    """校正步驟 3：印尺規。verify = 用目前標籤寬度印，確認內容是否從紙的左邊緣開始。"""
    verify = bool((request.get_json(force=True) or {}).get("verify"))
    ok, message = _send_job(
        "校正尺規（驗證）" if verify else "校正尺規",
        lambda cfg: ezpl.build_ruler(cfg, cfg["label"]["width_mm"] if verify else None),
        name="ruler",
    )
    return jsonify(ok=ok, message=message)


@app.post("/api/calibrate/width")
def calibrate_width():
    """由尺規左右兩端看到的數字算出紙寬；寬度改了，左右偏移歸零（之後用驗證尺規微調）。"""
    data = request.get_json(force=True) or {}
    left, right = _number(data, "left"), _number(data, "right")
    head = settings.load()["printer"]["head_width_mm"]
    if not 0 <= left < right <= head:
        return jsonify(ok=False, message=f"左邊數字要小於右邊，且都在 0～{head} 之間"), 400
    width = round((right - left) * 2) / 2
    cfg, err = _save_changes({"label": {"width_mm": width, "offset_x_mm": 0}})
    if err:
        return jsonify(ok=False, message=err), 400
    return jsonify(ok=True, width=width, message=f"標籤寬度已設為 {width:g}mm（左右偏移歸零）")


@app.post("/api/calibrate/offset")
def calibrate_offset():
    """驗證尺規左邊緣看到的數字 = 內容要往右移多少 mm。"""
    left = _number(request.get_json(force=True) or {}, "left")
    cfg, err = _save_changes({"label": {"offset_x_mm": left}})
    if err:
        return jsonify(ok=False, message=err), 400
    return jsonify(ok=True, message=f"左右偏移已設為 {left:g}mm")


@app.get("/api/usb-printers")
def usb_printers():
    """Windows 已安裝的印表機名稱，給設定頁的 USB 印表機下拉選單。"""
    try:
        return jsonify([name for name, _ in winprint.list_printers()])
    except OSError:
        return jsonify([])


@app.get("/api/diagnostics")
def get_diagnostics():
    with printer_lock:
        return jsonify(text=diagnostics.build_report())


@app.get("/api/settings")
def get_settings():
    return jsonify(settings.load())


@app.post("/api/settings")
def save_settings():
    cfg, err = _save_changes(request.get_json(force=True))  # 印表機 IP 或連線方式可能換了，重新判斷狀態查詢
    if err:
        return jsonify(ok=False, message=err), 400
    return jsonify(ok=True, settings=cfg)


@app.get("/api/logos")
def logos():
    return jsonify(settings.list_logos())


@app.post("/api/logos")
def upload_logo():
    f = request.files.get("file")
    name = (request.form.get("name") or "").strip()
    if not f or not name:
        return jsonify(ok=False, message="請選擇圖檔並填寫品牌名稱"), 400
    if re.search(r'[\\/:*?"<>|]', name):
        return jsonify(ok=False, message='品牌名稱不可包含 \\ / : * ? " < > |'), 400
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".bmp"):
        return jsonify(ok=False, message="只接受 PNG / JPG / BMP"), 400
    os.makedirs(settings.LOGO_DIR, exist_ok=True)
    old = settings.find_logo(name)
    if old:
        os.remove(old)
    f.save(os.path.join(settings.LOGO_DIR, name + ext))
    return jsonify(ok=True, logos=settings.list_logos())


@app.get("/api/logos/<name>.png")
def logo_image(name):
    path = settings.find_logo(name)
    if not path:
        abort(404)
    return send_file(path, max_age=0)


@app.get("/api/template")
def template():
    return send_file(
        io.BytesIO(template_bytes()),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="標籤範本.xlsx",
    )


@app.get("/api/log")
def log():
    if not os.path.exists(settings.LOG_PATH):
        return jsonify([])
    with open(settings.LOG_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return jsonify(rows[-200:][::-1])


@app.get("/api/version")
def get_version():
    return jsonify(version=VERSION)


@app.get("/api/update/check")
def update_check():
    try:
        return jsonify(ok=True, **updater.check())
    except updater.UpdateError as e:
        return jsonify(ok=False, current=VERSION, message=str(e))


@app.post("/api/update/install")
def update_install():
    if printer_lock.locked():
        return jsonify(ok=False, message="印表機正在工作，請等列印完成再更新")
    try:
        latest = updater.install()
    except updater.UpdateError as e:
        return jsonify(ok=False, message=str(e))
    return jsonify(ok=True, latest=latest, message=f"已下載 {latest}，程式即將關閉並自動重新啟動")


if __name__ == "__main__":
    settings.load()  # 第一次執行時建立 config.json
    print(f"\n標籤列印工具 {VERSION} 已啟動：\n  本機：http://localhost:{PORT}\n  其他電腦：http://{diagnostics.lan_ip()}:{PORT}\n關閉此視窗即停止。\n")
    if getattr(sys, "frozen", False):  # exe 版沒有 start.bat 幫忙開瀏覽器
        threading.Timer(1.5, webbrowser.open, [f"http://localhost:{PORT}"]).start()
    app.run(host="0.0.0.0", port=PORT)
