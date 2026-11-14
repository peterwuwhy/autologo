"""讀工作單（Google 試算表匯出的 xlsx），並把每一列對到布標檔。

工作單每個工作表是一天（名稱如 1151006），格式：
  第 1～2 列是日期標題；接著一列欄名（傢俱行、品牌、尺寸、規格、布號、…、數量、備註），最左邊沒有欄名的是交期。
  同一家傢俱行的後續列，傢俱行和品牌會留白；相同內容的儲存格常常合併。
"""
import io
import json
import os
import re

from openpyxl import load_workbook

import ezp
import gdrive
import settings

MAP_PATH = os.path.join(settings.BASE_DIR, "ezp_map.json")          # 人工選過的「品牌+規格 → 布標檔」
PRINTED_PATH = os.path.join(settings.BASE_DIR, "ezp_printed.json")  # 已列印的列
MAX_COPIES = 500
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[xX×*＊]\s*(\d+(?:\.\d+)?)\s*(尺)?\s*$")
# 常見的同音／異體字，比對時視為相同
_VARIANTS = str.maketrans({"綿": "棉", "璨": "燦", "臺": "台", "蓆": "席", "（": "(", "）": ")", "－": "-", "─": "-"})


def _text(v):
    if v is None:
        return ""
    if hasattr(v, "strftime"):  # 交期被試算表存成日期
        return f"{v.month}/{v.day}"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def norm(s):
    """比對用：小寫、去空白與標點、統一異體字。"""
    return re.sub(r"[\s\-_.,、，。:：()\[\]/\\]+", "", str(s).translate(_VARIANTS).lower())


def keys(name):
    """一個名稱的各種叫法：整串，以及用括號／連字號分開的每一段。
    例：「榮耀之星(璀璨之光)」→ {榮耀之星璀燦之光, 榮耀之星, 璀燦之光}。"""
    full = norm(name)
    parts = {norm(p) for p in re.split(r"[()\-/、,，]", str(name).translate(_VARIANTS))}
    return full, {p for p in parts if len(p) >= 2} | {full}


def file_title(filename):
    """布標檔名去掉 .ezp（有些檔名是「夢幻三線ezp.ezp」）。"""
    title = re.sub(r"\.ezp$", "", filename, flags=re.I)
    return re.sub(r"ezp$", "", title, flags=re.I).strip() or title


# ---------- 讀工作單 ----------

def open_book(data):
    try:
        return load_workbook(io.BytesIO(data), data_only=True)
    except Exception as e:
        raise gdrive.SourceError(f"工作單檔案打不開：{e}") from None


def read_tab(ws):
    """回傳 {"title", "rows": [...], "errors": [...]}。一列 = 工作單上的一筆（數量合併的多列算一筆）。"""
    merged = {}  # (列, 欄) → (左上角的值, 是不是左上角)
    for rng in ws.merged_cells.ranges:
        top = ws.cell(rng.min_row, rng.min_col).value
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                merged[(r, c)] = (top, (r, c) == (rng.min_row, rng.min_col))

    def cell(r, c):
        return merged[(r, c)][0] if (r, c) in merged else ws.cell(r, c).value

    header_row, cols = None, {}
    for r in range(1, min(ws.max_row, 15) + 1):
        names = {re.sub(r"\s+", "", _text(ws.cell(r, c).value)): c for c in range(1, ws.max_column + 1)}
        names.pop("", None)
        if "品牌" in names and "尺寸" in names and "規格" in names:
            header_row, cols = r, names
            break
    day = cell(1, 2) or cell(1, 1)
    title = f"{day.year}年{day.month}月{day.day}日" if hasattr(day, "strftime") else _text(day)
    if header_row is None:
        return {"title": title, "rows": [], "errors": ["這個工作表找不到欄名列（需要有「品牌」「尺寸」「規格」）"]}
    date_col = next((c for c in range(1, cols["品牌"]) if c not in cols.values()), None)

    def get(r, name):
        return _text(cell(r, cols[name])) if name in cols else ""

    rows, errors = [], []
    shop = brand = ""
    for r in range(header_row + 1, ws.max_row + 1):
        size, spec = get(r, "尺寸"), get(r, "規格")
        if not size and not spec:
            continue
        row_shop, row_brand = get(r, "傢俱行"), get(r, "品牌")
        if row_shop or row_brand:   # 新的一家／新的品牌；兩格都空白才沿用上一列
            shop, brand = row_shop or shop, row_brand
        qty_pos = (r, cols.get("數量", 0))
        if qty_pos in merged and not merged[qty_pos][1]:
            continue                # 數量和上面幾列合併：已經算在第一列
        qty_text = get(r, "數量")
        try:
            qty = int(float(qty_text)) if qty_text else 1
        except ValueError:
            qty = 0
        if not 0 < qty <= MAX_COPIES:
            errors.append(f"第 {r} 列（{spec}）：數量「{qty_text}」不是 1～{MAX_COPIES} 的整數，已略過")
            continue
        rows.append({
            "row": r, "date": _text(cell(r, date_col)) if date_col else "",
            "shop": shop, "brand": brand, "size": size, "spec": spec,
            "cloth": get(r, "布號"), "qty": qty, "note": get(r, "備註"),
        })
    return {"title": title, "rows": rows, "errors": errors}


# ---------- 比對 ----------

def parse_pairs(text):
    """設定頁的「左=右」多行文字 → dict。"""
    out = {}
    for line in str(text or "").replace("，", "\n").replace(",", "\n").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def size_table(cfg):
    out = {}
    for k, v in parse_pairs(cfg["source"]["size_cm"]).items():
        try:
            out[f"{float(k):g}"] = float(v) if "." in v else int(v)
        except ValueError:
            pass
    return out


def parse_feet(size):
    """工作單的尺寸「5x6.2」→ (5.0, 6.2)；不是「寬x長」的寫法（3.5尺、113x90x10cm）回傳 None。"""
    m = _SIZE.match(size or "")
    if not m:
        return None
    w, l = float(m.group(1)), float(m.group(2))
    return (w, l) if w < 30 and l < 30 else None


def find_brand(brand, folders, aliases):
    """工作單的品牌 → 布標資料夾名稱（品牌互相包含就算，例如「黛比」對「DEBBIE黛比」）。"""
    b = norm(brand)
    if not b:
        return None
    wanted = {b} | {norm(v) for k, v in aliases.items() if norm(k) == b} | {norm(k) for k, v in aliases.items() if norm(v) == b}
    for folder in folders:
        f = norm(folder)
        if any(w and (w in f or f in w) for w in wanted):
            return folder
    return None


def match_file(spec, files):
    """規格 → (檔名, 把握程度)。"exact" 名稱相同、"guess" 名稱互相包含（要人確認）、None 找不到。
    有兩個檔案一樣像時不猜，回傳 (None, None)。"""
    full, parts = keys(spec)
    if not full:
        return None, None
    scored = []
    for name in files:
        f_full, f_parts = keys(file_title(name))
        if f_full == full:
            score = 3
        elif parts & f_parts:
            score = 2
        elif any(len(a) >= 2 and len(b) >= 2 and (a in b or b in a) for a in parts for b in f_parts):
            score = 1
        else:
            continue
        scored.append((score, name))
    if not scored:
        return None, None
    best = max(s for s, _ in scored)
    top = [n for s, n in scored if s == best]
    if len(top) > 1:
        return None, None
    return top[0], "exact" if best >= 2 else "guess"


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def map_key(folder, spec):
    return f"{folder}|{norm(spec)}"


def remember(folder, spec, filename):
    """記住人工選的布標，下次同品牌同規格自動帶出。"""
    saved = _load_json(MAP_PATH)
    if saved.get(map_key(folder, spec)) != filename:
        saved[map_key(folder, spec)] = filename
        _save_json(MAP_PATH, saved)


def row_key(tab, row):
    """辨識「同一筆」：工作表 + 列號 + 內容。內容改了就視為新的一筆。"""
    return f'{tab}|{row["row"]}|{norm(row["brand"])}|{norm(row["spec"])}|{norm(row["size"])}|{row["qty"]}'


def mark_printed(tab, row, when):
    printed = _load_json(PRINTED_PATH)
    printed[row_key(tab, row)] = when
    _save_json(PRINTED_PATH, printed)


def annotate(tab, rows, cfg):
    """替每一列加上比對結果：
    folder 品牌資料夾、files 該品牌所有布標、file 選中的布標、match（exact / saved / guess / none / nobrand）、
    size_text 要印的尺寸文字（算不出來是空字串）、printed 上次列印時間。"""
    by_brand = {}
    for e in gdrive.library():
        by_brand.setdefault(e["brand"], {})[e["name"]] = e["path"]
    aliases = parse_pairs(cfg["source"]["brand_aliases"])
    to_cm = size_table(cfg)
    saved, printed = _load_json(MAP_PATH), _load_json(PRINTED_PATH)
    templates = {}
    for row in rows:
        folder = find_brand(row["brand"], by_brand, aliases)
        files = by_brand.get(folder, {})
        row.update(folder=folder or "", files=sorted(files), file="", size_text="", size_old="",
                   printed=printed.get(row_key(tab, row), ""))
        if not folder:
            row["match"] = "nobrand"
            continue
        name = saved.get(map_key(folder, row["spec"]))
        if name in files:
            row["match"] = "saved"
        else:
            name, how = match_file(row["spec"], files)
            row["match"] = how or "none"
        if not name:
            continue
        row["file"] = name
        try:
            if files[name] not in templates:
                templates[files[name]] = ezp.load(files[name])
            tpl = templates[files[name]]
        except (OSError, ezp.EzpError):
            continue
        row["size_old"] = ezp.size_line(tpl) or ""
        row["size_text"] = ezp.size_text(tpl, parse_feet(row["size"]), to_cm) or ""
    return rows
