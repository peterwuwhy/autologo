"""把點陣圖包成 GoDEX EZPL 指令並送到印表機（網路 TCP 9100，或 USB 經 Windows 列印佇列）。

依據 GoDEX EZPL Programmer's Manual：
- 每行指令以 CR LF 結束（手冊範例皆為 DOS 文字檔；資料長度不含換行）。
- Q 指令 "Qx,y,width,height"：x,y 是圖的「左下角」座標；width 單位 byte；
  下一行起是 width x height bytes 的點陣資料，每 8 點一個 byte，最高位元在左。
- BMP 方式："~EB,name,size" 下載 BMP 到印表機記憶體，再於 ^L 之後用 "Yx,y,name" 列印（x,y 為左上角）。
- 每張標籤都送一組完整的固定設定（同 GoLabel），不沿用印表機裡殘留的左邊界、位移、旋轉等設定。
"""
import datetime
import io
import json
import os
import re
import socket

from PIL import Image, ImageDraw

import settings
import winprint
from label_render import mm_to_dots, render

CRLF = b"\r\n"
_INVERT = bytes(b ^ 0xFF for b in range(256))
LAST_JOB_PATH = os.path.join(settings.OUTPUT_DIR, "last_job.json")
IMAGE_NAME = "AUTOLOGO"     # BMP 方式存在印表機裡的圖檔名稱
Q_MAX_BYTES = 32 * 1024     # 單一 Q 指令的點陣資料上限，超過就切段
TEST_IMAGE_NAME = "AUTOTEST"

# ~S,STATUS 回傳碼（EZPL 手冊 "~S,CHECK - Status immediate response command"）
STATUS_CODES = {
    "00": "正常待機",
    "01": "缺紙或卡紙",
    "02": "缺紙或卡紙",
    "03": "碳帶用完",
    "04": "上蓋或印字頭沒有蓋好",
    "05": "回收器已滿",
    "06": "記憶體已滿",
    "07": "找不到檔案",
    "08": "檔名重複",
    "09": "指令語法錯誤",
    "10": "裁刀卡紙",
    "11": "找不到擴充記憶體",
    "20": "暫停中",
    "21": "設定模式中",
    "22": "鍵盤模式中",
    "50": "列印中",
    "60": "資料處理中",
}
STATUS_ADVICE = {
    "01": "確認標籤紙有裝好；有裝好的話，到「診斷」按「紙張自動偵測」校正間距感測器。",
    "02": "確認標籤紙有裝好；有裝好的話，到「診斷」按「紙張自動偵測」校正間距感測器。",
    "03": "沒有裝碳帶 → 設定頁「列印方式」改成「熱感」；有裝碳帶 → 檢查是否用完或裝反。",
    "04": "把上蓋蓋好、印字頭壓下。",
    "06": "到「診斷」按「清除錯誤」後再試；仍發生請回報。",
    "09": "印表機看不懂送出的指令，請到「診斷」按「綜合測試」並回報結果。",
    "20": "按一下印表機 FEED 鍵，或到「診斷」按「清除錯誤」。",
}
READY_CODES = {"00", "50", "60"}

# 遠端控制指令（白名單）
COMMANDS = {
    "cancel": ("~S,CANCEL", "清除錯誤"),
    "sensor": ("~S,SENSOR", "紙張自動偵測"),
    "feed": ("~S,FEED", "送紙一次"),
    "selftest": ("~V", "列印自我測試頁"),
}


SEND_TIMEOUT = 30  # 送出整個 job 的最長時間（印表機忙碌時會放慢接收）
_immediate_on = set()  # 已送過 ^XSET,IMMEDIATE,1 的印表機（此為永久設定，不必每次寫入）


def _lines(*items):
    return b"".join(s.encode("ascii") + CRLF for s in items)


def _int(v):
    """設定值轉整數（EZPL 參數皆為整數）。"""
    return int(round(float(v)))


def _invert(img):
    """黑白反相。mode '1' 不能直接用 point(255 - v)，否則整張變白。"""
    return img.convert("L").point(lambda v: 255 - v).convert("1", dither=Image.Dither.NONE)


def _fit_to_label(img, x, y, cfg):
    """把圖裁到標籤範圍內，座標不小於 0；圖若超出標籤，印表機可能整張不印。"""
    p, lc = cfg["printer"], cfg["label"]
    label_w = mm_to_dots(lc["width_mm"], p["dpi"])
    label_h = mm_to_dots(lc["height_mm"], p["dpi"])
    left, top = max(0, -x), max(0, -y)
    x, y = max(0, x), max(0, y)
    right = min(img.width, left + label_w - x)
    bottom = min(img.height, top + label_h - y)
    if right <= left or bottom <= top:
        raise ValueError("偏移量太大，標籤圖完全落在標籤外。請調整設定頁的左右／上下偏移。")
    return img.crop((left, top, right, bottom)), x, y


def bitmap_bytes(img, invert=False):
    """mode '1' 圖 → (每列 bytes, 高度, 資料)。預設 1 = 黑點。"""
    w, h = img.size
    row_bytes = (w + 7) // 8
    if w % 8:  # 補白到 8 的倍數
        padded = Image.new("1", (row_bytes * 8, h), 1)
        padded.paste(img, (0, 0))
        img = padded
    # PIL mode '1' 的 tobytes(): 1 = 白；我們要 1 = 黑，所以預設反相
    raw = img.tobytes()
    if not invert:
        raw = raw.translate(_INVERT)
    return row_bytes, h, raw


def q_command(img, x, y_top, invert=False):
    """Q 指令：y 參數是左下角，所以用圖的上緣 + 圖高 - 1。
    大圖切成多段橫條分別送：G500 實測整張約 117KB 的單一 Q 圖印出空白，小圖則正常。"""
    row_bytes, h, data = bitmap_bytes(img, invert)
    rows = max(1, Q_MAX_BYTES // row_bytes)
    out = b""
    for top in range(0, h, rows):
        n = min(rows, h - top)
        out += _lines(f"Q{x},{y_top + top + n - 1},{row_bytes},{n}")
        out += data[top * row_bytes:(top + n) * row_bytes] + CRLF
    return out


def bmp_download(img, name, invert=False):
    """~EB 下載單色 BMP。先刪除同名圖檔，否則印表機會拒收（REPEAT FILE NAME）。"""
    if invert:
        img = _invert(img)
    buf = io.BytesIO()
    img.convert("1", dither=Image.Dither.NONE).save(buf, format="BMP")
    bmp = buf.getvalue()
    return _lines(f"~MDELG,{name}", f"~EB,{name},{len(bmp)}") + bmp + CRLF


def _setup_lines(cfg, copies):
    p, lc = cfg["printer"], cfg["label"]
    lines = [
        f'^Q{_int(lc["height_mm"])},{_int(lc["gap_mm"])}',
        f'^W{_int(lc["width_mm"])}',
        f'^H{_int(p["darkness"])}',
        f"^P{_int(copies)}",
        f'^S{_int(p["speed"])}',
    ]
    if p.get("print_mode") in ("T", "D"):
        lines.append(f'^A{p["print_mode"]}')
    # 以下為固定值，避免沿用印表機殘留設定：每張 1 份、左邊界 0、上下位移 0、
    # 不剝離、不裁切、不旋轉（~R 大於機種最大寬度即取消旋轉）
    lines += ["^C1", "^R0", "~Q+0", "^O0", "^D0", "~R200", "^L"]
    return lines


def _offsets(cfg):
    p, lc = cfg["printer"], cfg["label"]
    return mm_to_dots(lc["offset_x_mm"], p["dpi"]), mm_to_dots(lc["offset_y_mm"], p["dpi"])


def build_job(label, cfg, copies=None):
    p = cfg["printer"]
    copies = int(copies if copies is not None else label.get("數量", 1))
    img, x, y = _fit_to_label(render(label, cfg), *_offsets(cfg), cfg)
    invert = p.get("invert", False)
    if p.get("image_method") == "bmp":
        return (
            bmp_download(img, IMAGE_NAME, invert)
            + _lines(*_setup_lines(cfg, copies), f"Y{x},{y},{IMAGE_NAME}", "E")
        )
    return _lines(*_setup_lines(cfg, copies)) + q_command(img, x, y, invert) + _lines("E")


def _test_pattern(w=240, h=120):
    """左半部全黑、右半部只有外框，右上角一條黑槓：可看出黑白是否相反、圖是否上下顛倒。"""
    img = Image.new("1", (w, h), 1)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w // 2 - 1, h - 1], fill=0)
    d.rectangle([w // 2, 0, w - 1, h - 1], outline=0, width=4)
    d.rectangle([w // 2, 0, w - 1, 15], fill=0)
    return img


def _test_label(cfg, title, body):
    """一張測試標籤：外框 + 標題 + 設定摘要 + body（label format 指令）。"""
    p, lc = cfg["printer"], cfg["label"]
    dpi = p["dpi"]
    m = mm_to_dots(lc["margin_mm"], dpi)
    ox, oy = (max(0, v + m) - m for v in _offsets(cfg))  # 座標不小於 0
    w, h = mm_to_dots(lc["width_mm"], dpi), mm_to_dots(lc["height_mm"], dpi)
    mode = {"T": "TT", "D": "DT"}.get(p.get("print_mode"), "PRINTER")
    head = _lines(
        *_setup_lines(cfg, 1),
        f"R{ox + m},{oy + m},{ox + w - m},{oy + h - m},6,6",
        f"AD,{ox + m + 24},{oy + m + 24},1,1,0,0,{title}",
        f'AD,{ox + m + 24},{oy + m + 84},1,1,0,0,{lc["width_mm"]}x{lc["height_mm"]}mm GAP {lc["gap_mm"]}',
        f'AD,{ox + m + 24},{oy + m + 144},1,1,0,0,H{p["darkness"]} S{p["speed"]} MODE {mode}',
    )
    return head + body + _lines("E")


def build_basic_test(cfg):
    """不含圖片的測試標籤：外框 + 印表機內建英文字型。"""
    return _test_label(cfg, "GODEX TEST OK", b"")


def build_full_test(cfg):
    """綜合測試：連續印 3 張，一張照片就能判斷問題在哪一層。
    1/3 只有文字；2/3 BMP 方式的測試圖；3/3 Q 方式的測試圖（應在參考線下方）。"""
    p = cfg["printer"]
    dpi = p["dpi"]
    m = mm_to_dots(cfg["label"]["margin_mm"], dpi)
    ox, oy = (max(0, v + m) - m for v in _offsets(cfg))  # 與 _test_label 相同的起點
    invert = p.get("invert", False)
    pattern = _test_pattern()
    px, py = ox + m + 24, oy + m + 260  # 測試圖左上角
    caption = lambda text: _lines(f"AD,{px},{py - 50},1,1,0,0,{text}")
    ref_line = _lines(f"R{px - 16},{py - 4},{px + pattern.width + 16},{py - 1},2,2")

    label1 = _test_label(cfg, "TEST 1/3 TEXT ONLY", b"")
    # 標籤上的文字避免 ~ 與 ^（EZPL 的指令／變數符號）
    label2 = bmp_download(pattern, TEST_IMAGE_NAME, invert) + _test_label(
        cfg, "TEST 2/3 BMP EB+Y",
        caption("BMP PATTERN BELOW LINE") + ref_line + _lines(f"Y{px},{py},{TEST_IMAGE_NAME}"),
    )
    label3 = _test_label(
        cfg, "TEST 3/3 Q COMMAND",
        caption("Q PATTERN BELOW LINE") + ref_line + q_command(pattern, px, py, invert),
    )
    return label1 + label2 + label3


def build_ruler(cfg, width_mm=None):
    """校正用尺規：每 5mm 一條刻度、每 10mm 標數字，底下一條橫線。
    width_mm 預設為印字頭寬度（^W = 印字頭寬，座標從印字頭最左邊算起）；
    給標籤寬度時用來驗證內容是否從紙的左邊緣開始。"""
    p = cfg["printer"]
    width = width_mm or p["head_width_mm"]
    dpi = p["dpi"]
    lines = [
        "^Q25,0", f"^W{_int(width)}", f'^H{_int(p["darkness"])}', "^P1", f'^S{_int(p["speed"])}',
    ]
    if p.get("print_mode") in ("T", "D"):
        lines.append(f'^A{p["print_mode"]}')
    lines += ["^C1", "^R0", "~Q+0", "^O0", "^D0", "~R200", "^L"]
    for mm in range(0, int(width) + 1, 5):
        x = mm_to_dots(mm, dpi)
        tall = mm % 10 == 0
        lines.append(f"R{x},0,{x + 3},{80 if tall else 40},3,3")
        if tall:
            lines.append(f"AB,{x + 6},90,1,1,0,0,{mm}")
    lines += [f"R0,150,{mm_to_dots(width, dpi) - 1},153,3,3", "E"]
    return _lines(*lines)


def media_setup(cfg):
    """清除錯誤並寫入紙張設定（不列印）：標籤長度／間距（0 = 連續紙）、寬度、列印方式。"""
    p, lc = cfg["printer"], cfg["label"]
    lines = ["~S,CANCEL", f'^Q{_int(lc["height_mm"])},{_int(lc["gap_mm"])}', f'^W{_int(lc["width_mm"])}']
    if p.get("print_mode") in ("T", "D"):
        lines.append(f'^A{p["print_mode"]}')
    return _lines(*lines)


def describe_job(job):
    """把指令轉成可閱讀的文字：點陣／BMP 資料只列長度、黑點比例與前 16 bytes。"""
    if not job:
        return "（沒有產生指令）"
    out, i = [], 0

    def skip_crlf(pos):
        return pos + 2 if job[pos:pos + 2] == CRLF else pos

    while i < len(job):
        end = job.find(b"\r", i)
        if end == -1:
            end = len(job)
        line = job[i:end].decode("ascii", "replace")
        i = end + (2 if job[end:end + 2] == CRLF else 1)
        if line:
            out.append(line)
        size = None
        if line.startswith("Q") and line.count(",") == 3:
            _, _, row_bytes, h = (int(v) for v in line[1:].split(","))
            size, kind = row_bytes * h, f"點陣資料（應為 {row_bytes}x{h}={row_bytes * h}）"
        elif line.startswith("~E") and line.count(",") == 2:
            size, kind = int(line.split(",")[2]), "BMP 檔"
        if size is not None:
            data = job[i:i + size]
            black = sum(bin(b).count("1") for b in data)
            out.append(
                f"<{kind} {len(data)} bytes，1 的比例 {100 * black / max(len(data) * 8, 1):.1f}%，"
                f"前 16 bytes：{data[:16].hex(' ')}>"
            )
            i = skip_crlf(i + size)
    return "\n".join(out)


def save_last_job(kind, job, ok, message, label=None):
    os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
    info = {
        "時間": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "類型": kind,
        "標籤": f'{label["型號"]} {label["寬"]}x{label["長"]} x{label.get("數量", 1)}' if label else "",
        "結果": "成功" if ok else "失敗",
        "訊息": message,
        "指令總長": len(job) if job else 0,
        "指令內容": describe_job(job),
    }
    with open(LAST_JOB_PATH, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)


def load_last_job():
    try:
        with open(LAST_JOB_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def send(job, cfg, name="label"):
    """送出一個 job。dry_run 時寫到 output/ 並回傳檔案路徑。"""
    p = cfg["printer"]
    if p.get("dry_run"):
        os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(settings.OUTPUT_DIR, f"{stamp}_{name}.prn")
        with open(path, "wb") as f:
            f.write(job)
        return f"已寫入 {os.path.relpath(path, settings.BASE_DIR)}"
    _send_raw(cfg, job)
    return "已送出"


def is_usb(cfg):
    return cfg["printer"].get("connection") == "usb"


def address(cfg):
    """給訊息用的印表機位址文字。"""
    p = cfg["printer"]
    return f'USB「{p["usb_name"]}」' if is_usb(cfg) else f'{p["ip"]}:{p["port"]}'


def _send_raw(cfg, data):
    """送出資料並正常關閉連線：先關閉傳送端，再讀完印表機回傳的內容，
    避免直接關閉時送出 RST，讓尚未送達的資料被丟棄。"""
    p = cfg["printer"]
    if is_usb(cfg):
        winprint.send_raw(p["usb_name"], data, timeout=SEND_TIMEOUT)
        return
    with socket.create_connection((p["ip"], int(p["port"])), timeout=5) as s:
        s.settimeout(SEND_TIMEOUT)
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        s.settimeout(1)
        try:
            while s.recv(4096):
                pass
        except OSError:
            pass


def test_connection(cfg):
    p = cfg["printer"]
    if is_usb(cfg):
        winprint.check(p["usb_name"])
        return
    with socket.create_connection((p["ip"], int(p["port"])), timeout=3):
        pass


def _ask(cfg, request, first_timeout=2, idle_timeout=0.5, max_bytes=8192):
    """送出查詢並讀回應：等第一個位元組最多 first_timeout 秒，之後閒置 idle_timeout 秒即結束。"""
    p = cfg["printer"]
    if is_usb(cfg):
        reply = winprint.ask(p["usb_name"], request, first_timeout, idle_timeout, max_bytes)
        return reply.decode("ascii", "replace").strip()
    with socket.create_connection((p["ip"], int(p["port"])), timeout=3) as s:
        s.sendall(request)
        s.settimeout(first_timeout)
        reply = b""
        try:
            while len(reply) < max_bytes:
                chunk = s.recv(4096)
                if not chunk:
                    break
                reply += chunk
                s.settimeout(idle_timeout)
        except socket.timeout:
            pass
    return reply.decode("ascii", "replace").strip()


def _status_line(reply):
    """從回應中找出狀態碼那一行（如 "00,00000"）。USB 讀取時前面可能夾著之前殘留的輸出（例如自我測試頁內容）。"""
    lines = [s.strip() for s in reply.splitlines()]
    return next((s for s in reversed(lines) if re.fullmatch(r"\d{2}(,\d+)?", s)), "")


def query_status(cfg, timeout=2):
    """回傳 {"code", "text", "raw"}；印表機沒回應時 code 為 None。連不上會丟 OSError。"""
    p = cfg["printer"]
    if is_usb(cfg):
        test_connection(cfg)  # 先確認 Windows 找得到印表機、沒有回報離線
        key = ("usb", p["usb_name"])
    else:
        key = (p["ip"], int(p["port"]))
    raw = ""
    if key in _immediate_on:
        raw = _status_line(_ask(cfg, _lines("~S,STATUS"), first_timeout=timeout))
    if not raw:  # 第一次查詢，或印表機重開機後立即回應被關掉
        raw = _status_line(_ask(cfg, _lines("^XSET,IMMEDIATE,1", "~S,STATUS"), first_timeout=timeout))
    if raw:
        _immediate_on.add(key)
    else:
        _immediate_on.discard(key)
    if not raw:
        return {"code": None, "text": "印表機沒有回傳狀態（連線正常，但可能不支援狀態查詢）", "raw": ""}
    code = raw.split(",")[0].strip()
    text = f"{code}：{STATUS_CODES.get(code, '未知狀態')}"
    if code in STATUS_ADVICE:
        text += f"。建議：{STATUS_ADVICE[code]}"
    return {"code": code, "text": text, "raw": raw}


def query_config(cfg):
    """讀取印表機內部設定（同自我測試頁）與韌體版本；不支援時回傳空字串。"""
    return _ask(cfg, _lines("^XGET,CONFIG", "~B"), first_timeout=3, idle_timeout=1)


def send_command(cfg, name):
    """送出 COMMANDS 中的控制指令。"""
    cmd, _ = COMMANDS[name]
    _send_raw(cfg, cmd.encode("ascii") + CRLF)
