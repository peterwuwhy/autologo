"""產生純文字診斷報告：讓現場人員一鍵複製，貼給 AI 或技術人員判讀問題。"""
import csv
import datetime
import hashlib
import json
import os
import platform
import socket
import sys
import time

import ezpl
import settings
from label_render import SAMPLE_LABEL, render, resolve_font
from version import VERSION

CODE_FILES = [
    "app.py", "ezpl.py", "winprint.py", "diagnostics.py", "settings.py", "label_render.py",
    "excel_reader.py", "make_template.py", "updater.py", "version.py", "templates/index.html", "static/app.js",
]


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "（無法取得）"


def _file_versions():
    out = []
    for rel in CODE_FILES:
        path = os.path.join(settings.RESOURCE_DIR, rel)
        if not os.path.exists(path):
            out.append(f"  {rel}：（找不到）")
            continue
        with open(path, "rb") as f:
            digest = hashlib.sha1(f.read()).hexdigest()[:8]
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
        out.append(f"  {rel}：{digest}（{mtime}）")
    return out


def _printer_section(cfg, checks):
    p = cfg["printer"]
    lines = [f"  位址：{ezpl.address(cfg)}"]
    if p.get("dry_run"):
        checks.append("測試模式開著：標籤只會存成檔案，不會送到印表機。要正式列印請到設定頁取消勾選。")
    start = time.monotonic()
    try:
        ezpl.test_connection(cfg)
        lines.append(f"  連線：成功（{(time.monotonic() - start) * 1000:.0f} ms）")
    except OSError as e:
        lines.append(f"  連線：失敗（{e}）")
        if ezpl.is_usb(cfg):
            checks.append("連不上印表機：確認印表機有開機、USB 線有插好，設定頁選的 USB 印表機名稱正確。")
        else:
            checks.append("連不上印表機：確認印表機有開機、網路線有插好、IP 位址正確。")
        return lines
    try:
        st = ezpl.query_status(cfg)
        lines.append(f'  狀態查詢回傳原文：{st["raw"] or "（無回應）"}')
        lines.append(f'  狀態：{st["text"]}')
        if st["code"] is None:
            checks.append("印表機不回傳狀態，無法得知是否報錯；請現場看 STATUS 燈的顏色，並拍照。")
        elif st["code"] not in ezpl.READY_CODES:
            checks.append(f'印表機目前回報錯誤 → {st["text"]}')
    except OSError as e:
        lines.append(f"  狀態查詢失敗：{e}")
    try:
        conf = ezpl.query_config(cfg)
        lines.append("  印表機內部設定（^XGET,CONFIG、~B）：")
        lines += [f"    {s}" for s in conf.splitlines()] if conf else ["    （印表機沒有回傳；可到「診斷」按「列印自我測試頁」並拍照）"]
    except OSError as e:
        lines.append(f"  讀取內部設定失敗：{e}")
    return lines


def _setting_checks(cfg, checks):
    p, lc = cfg["printer"], cfg["label"]
    if not p.get("print_mode"):
        checks.append("列印方式為「維持印表機設定」：不知道印表機目前是熱感還是熱轉印。"
                      "若印出全白，請確認有沒有裝碳帶，並在設定頁明確選擇列印方式。")
    if lc["width_mm"] > p["head_width_mm"]:
        checks.append(f'標籤寬度 {lc["width_mm"]}mm 超過印字頭可印寬度 {p["head_width_mm"]}mm，右側會被切掉。')
    if lc["height_mm"] <= 0 or lc["gap_mm"] < 0:
        checks.append("標籤高度或間距數值不正確。")
    if p.get("invert"):
        checks.append("「黑白反相」已勾選：若印出整張黑或全白，請試著取消勾選。")


def _label_section(cfg, checks):
    method = cfg["printer"].get("image_method", "q")
    lines = [f'  送圖方式：{ {"q": "Q 指令", "bmp": "BMP 下載（~EB + Y）"}.get(method, method) }']
    try:
        font_path, font_index = resolve_font(cfg)
        lines.append(f"  字型：{font_path}（index {font_index}）")
        img = render(SAMPLE_LABEL, cfg)
        dpi = cfg["printer"]["dpi"]
        lines.append(f"  範例標籤圖：{img.width} x {img.height} dots"
                     f"（{img.width * 25.4 / dpi:.1f} x {img.height * 25.4 / dpi:.1f} mm，{dpi} dpi）")
    except Exception as e:
        lines.append(f"  產生標籤圖失敗：{e}")
        checks.append(f"無法產生標籤圖：{e}")
    return lines


def _last_job_section(checks):
    job = ezpl.load_last_job()
    if not job:
        return ["  （尚未送出過任何指令）"]
    if job.get("結果") == "失敗":
        checks.append(f'最近一次{job.get("類型", "")}失敗：{job.get("訊息", "")}')
    lines = [f"  {k}：{job.get(k, '')}" for k in ("時間", "類型", "標籤", "結果", "訊息", "指令總長")]
    lines.append("  指令內容：")
    lines += [f"    {s}" for s in (job.get("指令內容") or "").splitlines()]
    return lines


def _recent_log(n=10):
    if not os.path.exists(settings.LOG_PATH):
        return ["  （尚無紀錄）"]
    with open(settings.LOG_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    return [f"  {' | '.join(r)}" for r in rows[:1] + rows[1:][-n:]]


def build_report():
    cfg = settings.load()
    checks = []
    printer = _printer_section(cfg, checks)
    _setting_checks(cfg, checks)
    label = _label_section(cfg, checks)
    last_job = _last_job_section(checks)

    parts = [
        "=== 床墊標籤列印工具 診斷報告 ===",
        f"程式版本：{VERSION}（{'exe 版' if getattr(sys, 'frozen', False) else '原始碼版'}）",
        f'產生時間：{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f"電腦：{platform.platform()}，Python {sys.version.split()[0]}，本機 IP {lan_ip()}",
        "程式檔版本：",
        *_file_versions(),
        "",
        "[印表機]",
        *printer,
        "",
        "[自動檢查]",
        *([f"  - {c}" for c in checks] or ["  沒有發現問題"]),
        "",
        "[最近一次送出的指令]",
        *last_job,
        "",
        "[標籤圖]",
        *label,
        "",
        "[設定 config.json]",
        *[f"  {s}" for s in json.dumps(cfg, ensure_ascii=False, indent=2).splitlines()],
        "",
        "[最近 10 筆列印紀錄]",
        *_recent_log(),
        "",
        "=== 報告結束 ===",
    ]
    return "\n".join(parts)
