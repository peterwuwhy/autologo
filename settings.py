"""讀寫 config.json。缺少的欄位自動補上預設值。"""
import copy
import json
import os
import sys

if getattr(sys, "frozen", False):
    # 打包成 exe：網頁檔在 PyInstaller 的 _internal 資料夾；設定、Logo、紀錄放在 exe 旁邊
    BASE_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOGO_DIR = os.path.join(BASE_DIR, "logos")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
LOG_PATH = os.path.join(BASE_DIR, "print_log.csv")

DEFAULTS = {
    "printer": {
        "connection": "network",  # network = 網路 TCP；usb = 經 Windows 列印佇列送 RAW
        "usb_name": "",      # USB 時使用的 Windows 印表機名稱，例如 "Godex G500"
        "ip": "192.168.1.1",
        "port": 9100,
        "dpi": 203,
        "head_width_mm": 104,  # 印字頭可印寬度（G500 為 104mm），校正尺規印這麼寬
        "darkness": 10,      # ^H 濃度 0~19
        "speed": 4,          # ^S 速度
        "invert": False,     # 若印出來黑白相反，設為 true
        "dry_run": False,    # true = 不送印表機，改寫 .prn 檔到 output/
        "print_mode": "",    # "" = 維持印表機設定；"T" = 熱轉印 ^AT（需碳帶）；"D" = 熱感 ^AD
        "image_method": "q", # q = Q 指令直接送點陣（GoDEX SDK 方式）；bmp = ~EB 下載 BMP 再用 Y 列印
    },
    "label": {
        "width_mm": 100,
        "height_mm": 150,
        "gap_mm": 3,         # 0 = 連續紙（^Qx,0，不找間距）
        "offset_x_mm": 0,
        "offset_y_mm": 0,
        "margin_mm": 4,
        "logo_top_mm": 0,    # Logo 上方額外留白（邊界之外再加）
        "logo_height_ratio": 0.32,  # logo 佔標籤高度比例
        "max_font_mm": 4.5,
    },
    "font": {
        "path": "",   # 空白 = 自動尋找系統中文字型
        "index": 0,
    },
    "company": {
        "製造廠商": "快樂床業有限公司",
        "廠商地址": "新北市新店區安康路二段3-2號",
        "統一編號": "34609285",
        "服務專線": "(02)2211-7010",
        "製造地": "台灣",
    },
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


# 數值設定的範圍：(區段, 欄位, 名稱, 最小, 最大, 是否整數)
NUMBER_RULES = [
    ("printer", "port", "連接埠", 1, 65535, True),
    ("printer", "dpi", "解析度", 100, 600, True),
    ("printer", "head_width_mm", "印字頭寬度", 20, 220, False),
    ("printer", "darkness", "濃度", 0, 19, True),
    ("printer", "speed", "速度", 1, 7, True),
    ("label", "width_mm", "標籤寬", 10, 120, False),
    ("label", "height_mm", "標籤高", 10, 1000, False),
    ("label", "gap_mm", "間距", 0, 30, False),
    ("label", "offset_x_mm", "左右偏移", -50, 50, False),
    ("label", "offset_y_mm", "上下偏移", -50, 50, False),
    ("label", "margin_mm", "邊界", 0, 30, False),
    ("label", "logo_top_mm", "Logo 上方留白", 0, 500, False),
    ("label", "logo_height_ratio", "Logo 高度比例", 0, 0.8, False),
    ("label", "max_font_mm", "最大字高", 1, 30, False),
    ("font", "index", "字型編號", 0, 99, True),
]
CHOICE_RULES = [
    ("printer", "connection", "連線方式", ("network", "usb")),
    ("printer", "print_mode", "列印方式", ("", "T", "D")),
    ("printer", "image_method", "送圖方式", ("q", "bmp")),
]


def validate(cfg):
    """檢查並整理設定值（數字轉成正確型別）。回傳錯誤訊息清單，空清單代表正確。"""
    errors = []
    for section, key, name, lo, hi, integer in NUMBER_RULES:
        v = cfg[section].get(key)
        try:
            n = float(v)
        except (TypeError, ValueError):
            errors.append(f"{name}不能空白，必須是數字")
            continue
        if n != n or not lo <= n <= hi:  # n != n 代表 NaN
            errors.append(f"{name}必須介於 {lo} 到 {hi}")
            continue
        cfg[section][key] = int(round(n)) if integer or n.is_integer() else n
    for section, key, name, choices in CHOICE_RULES:
        if cfg[section].get(key) not in choices:
            errors.append(f"{name}的值不正確")
    ip = str(cfg["printer"].get("ip") or "").strip()
    usb_name = str(cfg["printer"].get("usb_name") or "").strip()
    if cfg["printer"].get("connection") == "usb":
        if not usb_name:
            errors.append("請選擇 USB 印表機")
    elif not ip or " " in ip:
        errors.append("印表機 IP 位址不正確")
    cfg["printer"]["ip"] = ip
    cfg["printer"]["usb_name"] = usb_name
    return errors


def load():
    if not os.path.exists(CONFIG_PATH):
        save(DEFAULTS)
        return copy.deepcopy(DEFAULTS)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return _merge(DEFAULTS, json.load(f))


def save(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def find_logo(name):
    """依品牌名稱找 logos/ 底下的圖檔，找不到回傳 None。"""
    for ext in (".png", ".jpg", ".jpeg", ".bmp"):
        p = os.path.join(LOGO_DIR, name + ext)
        if os.path.exists(p):
            return p
    return None


def list_logos():
    if not os.path.isdir(LOGO_DIR):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(LOGO_DIR)
        if f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp"))
    )
