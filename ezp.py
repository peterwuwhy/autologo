"""讀取 GoLabel 的 .ezp 布標檔，換掉「尺寸」那一行後畫成黑白點陣圖。

.ezp 是 GoLabel 直接把記憶體結構寫出來的二進位檔（沒有公開規格），以下欄位位置是
比對 39 個實際布標檔得出的：
- 檔頭 0x358 / 0x35C：標籤長度、寬度（mm）。
- 0x1D9C 起是物件，一筆接一筆：[類型 u32][大小 u32][1 u32] + 內容（大小 + 60 bytes）。
  內容 +0x00 名稱、+0x50 x、+0x54 y（單位：印表機的點，203dpi 時 8 點 = 1mm）、+0x114 起各類型自己的欄位。
  - Window Text：+0x114 文字長度、+0x118 Windows LOGFONT（字高、粗細、字型名稱），文字（Big5）在最後。
  - Graphic：+0x114 顯示寬、高，+0x16C 起是單色 BMP。
  - Line：+0x114 長度、粗細。Rectangle：+0x114 寬、高、框線粗細。
"""
import io
import os
import re
import struct

from PIL import Image, ImageDraw, ImageFont

import settings
from label_render import mm_to_dots, resolve_font

OBJ_START = 0x1D9C
EXTRA = 60          # 每筆物件內容比「大小」欄位多出的固定長度
END_TYPE = 105      # QUERY：物件清單結束
SIZE_LINE = re.compile(r"(尺寸\s*[:：]\s*)([^\r\n]*)")
_NUM = r"\d+(?:\.\d+)?"
_SIZE_VALUE = re.compile(rf"({_NUM})(\s*(?:cm|公分)?\s*[×xX*＊]\s*)({_NUM})")


class EzpError(Exception):
    """布標檔讀不懂。訊息可直接顯示給使用者。"""


# ---------- 解析 ----------

def _cstr(data, pos, size, encoding="ascii"):
    return data[pos:pos + size].split(b"\0")[0].decode(encoding, "replace")


def _first_object(data):
    """物件清單的起點。正常是固定位置；位置不對時往後找第一筆合理的物件。"""
    def ok(pos):
        if pos + 16 > len(data):
            return False
        t, size, flag = struct.unpack_from("<III", data, pos)
        return flag == 1 and 0 < t < 200 and 0 < size < len(data) and data[pos + 12:pos + 13].isalpha()
    if ok(OBJ_START):
        return OBJ_START
    for pos in range(0x400, len(data) - 16):
        if ok(pos) and _cstr(data, pos + 12, 16) in ("Line", "Window Text", "Graphic", "Rectangle"):
            return pos
    raise EzpError("不是 GoLabel 的 .ezp 檔，或檔案版本不支援")


def parse(data):
    """回傳 {"width_mm", "height_mm", "objects": [...], "unsupported": [...]}。"""
    if data[4:12] != b"Ezpl Pro":
        raise EzpError("不是 GoLabel 的 .ezp 檔")
    height, width = struct.unpack_from("<ii", data, 0x358)
    objects, unsupported = [], []
    pos = _first_object(data)
    while pos + 12 <= len(data):
        kind, size, flag = struct.unpack_from("<III", data, pos)
        body = pos + 12
        end = body + size + EXTRA
        if flag != 1 or kind == END_TYPE or end > len(data) or size < 0x60:
            break
        name = _cstr(data, body, 32)
        x, y = struct.unpack_from("<ii", data, body + 0x50)
        if name == "Window Text":
            length = struct.unpack_from("<I", data, body + 0x114)[0]
            font_h, _, _, _, weight = struct.unpack_from("<5i", data, body + 0x118)
            objects.append({
                "type": "text", "x": x, "y": y,
                "height": abs(font_h), "bold": weight >= 600, "italic": bool(data[body + 0x12C]),
                "face": _cstr(data, body + 0x134, 32, "big5"),
                "text": data[end - length:end].decode("big5", "replace") if 0 < length <= size else "",
            })
        elif name == "Graphic":
            w, h, n = struct.unpack_from("<3i", data, body + 0x114)
            bmp = data[body + 0x16C:body + 0x16C + n]
            if bmp[:2] == b"BM":
                objects.append({"type": "graphic", "x": x, "y": y, "w": w, "h": h, "bmp": bmp})
            else:
                unsupported.append("Graphic（不是 BMP）")
        elif name == "Line":
            length, thick = struct.unpack_from("<2i", data, body + 0x114)
            objects.append({"type": "line", "x": x, "y": y, "w": length, "h": max(1, thick)})
        elif name == "Rectangle":
            w, h, thick = struct.unpack_from("<3i", data, body + 0x114)
            objects.append({"type": "rect", "x": x, "y": y, "w": w, "h": h, "thick": max(1, thick)})
        else:
            unsupported.append(name or f"類型 {kind}")
        pos = end
    if not objects:
        raise EzpError("布標檔裡沒有找到任何內容")
    return {"width_mm": width, "height_mm": height, "objects": objects, "unsupported": unsupported}


def load(path):
    with open(path, "rb") as f:
        return parse(f.read())


# ---------- 尺寸 ----------

def size_line(tpl):
    """布標上「尺寸:」後面的原始文字，沒有尺寸行回傳 None。"""
    for o in tpl["objects"]:
        if o["type"] == "text":
            m = SIZE_LINE.search(o["text"])
            if m:
                return m.group(2).strip()
    return None


def _fmt(n):
    return f"{n:g}"


def size_text(tpl, feet, to_cm):
    """依布標原本的寫法產生新的尺寸文字。feet = (寬, 長)，單位尺；to_cm = {尺: 公分}。
    布標原本寫尺（6×6.2尺）就填尺；原本寫公分（152cm×188cm）就換算成公分。
    換算表查不到、或布標沒有尺寸行時回傳 None（要人工填）。"""
    old = size_line(tpl)
    if old is None or not feet:
        return None
    m = _SIZE_VALUE.search(old)
    if not m:
        return None
    in_feet = float(m.group(1)) < 30 and float(m.group(3)) < 30
    if in_feet:
        w, l = feet
    else:
        w, l = to_cm.get(_fmt(feet[0])), to_cm.get(_fmt(feet[1]))
        if w is None or l is None:
            return None
    return old[:m.start()] + _fmt(w) + m.group(2) + _fmt(l) + old[m.end():]


def _with_size(text, size):
    return SIZE_LINE.sub(lambda m: m.group(1) + size, text, count=1) if size else text


# ---------- 字型 ----------

# GoLabel 的 Window Text 用 Windows 字型；這裡依字型名稱找檔案：(一般, 粗體)。
# 每項是依序嘗試的 (檔名, ttc 內編號)；最後幾個是 Mac 上預覽用的替代字型。
FACES = {
    "細明體": ([("mingliu.ttc", 0), ("Songti.ttc", 7)], []),
    "mingliu": ([("mingliu.ttc", 0), ("Songti.ttc", 7)], []),
    "新細明體": ([("mingliu.ttc", 1), ("Songti.ttc", 7)], []),
    "pmingliu": ([("mingliu.ttc", 1), ("Songti.ttc", 7)], []),
    "微軟正黑體": ([("msjh.ttc", 0), ("msjh.ttf", 0), ("STHeiti Light.ttc", 0)],
              [("msjhbd.ttc", 0), ("msjhbd.ttf", 0), ("STHeiti Medium.ttc", 0)]),
    "microsoft jhenghei": ([("msjh.ttc", 0), ("msjh.ttf", 0)], [("msjhbd.ttc", 0), ("msjhbd.ttf", 0)]),
    "標楷體": ([("kaiu.ttf", 0)], []),
    "arial": ([("arial.ttf", 0), ("Arial.ttf", 0)], [("arialbd.ttf", 0), ("Arial Bold.ttf", 0)]),
    "times new roman": ([("times.ttf", 0), ("Times New Roman.ttf", 0)],
                        [("timesbd.ttf", 0), ("Times New Roman Bold.ttf", 0)]),
}
# 英文字型沒有中文字時 Windows 會自動改用新細明體
CJK_FALLBACK = "新細明體"
FONT_DIRS = [
    os.path.join(settings.BASE_DIR, "fonts"),
    r"C:\Windows\Fonts",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
]
_font_cache = {}
_ratio_cache = {}


def _find_file(candidates):
    for name, index in candidates:
        for d in FONT_DIRS:
            path = os.path.join(d, name)
            if os.path.exists(path):
                return path, index
    return None


def _cell_ratio(path, index):
    """字型的 (上升, 下降) 佔 em 的比例，取 Windows 用的 usWinAscent / usWinDescent。
    LOGFONT 的字高是「上升 + 下降」的點數，要靠這個比例換算成字型大小。"""
    key = (path, index)
    if key in _ratio_cache:
        return _ratio_cache[key]
    ratio = None
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            base = 0
            if head[:4] == b"ttcf":
                count = struct.unpack(">I", head[8:12])[0]
                offsets = struct.unpack(f">{count}I", f.read(4 * count))
                base = offsets[min(index, count - 1)]
            f.seek(base)
            num = struct.unpack(">4xH6x", f.read(12))[0]
            tables = {}
            for _ in range(num):
                tag, _, off, _ = struct.unpack(">4sIII", f.read(16))
                tables[tag] = off
            f.seek(tables[b"head"] + 18)
            upm = struct.unpack(">H", f.read(2))[0]
            f.seek(tables[b"OS/2"] + 74)
            asc, desc = struct.unpack(">HH", f.read(4))
            if upm and asc + desc:
                ratio = (asc / upm, desc / upm)
    except (OSError, KeyError, struct.error):
        pass
    if ratio is None:  # 讀不到就用 FreeType 回報的值
        a, d = ImageFont.truetype(path, 1000, index=index).getmetrics()
        ratio = (a / 1000, d / 1000)
    _ratio_cache[key] = ratio
    return ratio


def _font(face, height, bold, cfg):
    """回傳 {"font", "ascent"(基線到字格頂端的點數), "fake_bold", "cjk", "found"}。"""
    key = (face.lower(), height, bold, cfg["font"].get("path"), cfg["font"].get("index"))
    if key in _font_cache:
        return _font_cache[key]
    regular, bold_files = FACES.get(face.lower(), ([], []))
    hit = _find_file(bold_files) if bold else None
    fake_bold = bold and hit is None
    hit = hit or _find_file(regular)
    found = hit is not None
    if not found:
        hit = resolve_font(cfg)
    path, index = hit
    asc, desc = _cell_ratio(path, index)
    size = max(4, round(height / (asc + desc)))
    font = ImageFont.truetype(path, size, index=index)
    info = {
        "font": font, "ascent": round(size * asc), "fake_bold": fake_bold, "found": found,
        "cjk": font.getmask("台").getbbox() is not None and bytes(font.getmask("台")) != bytes(font.getmask("\uffff")),
    }
    _font_cache[key] = info
    return info


def _is_cjk(ch):
    return ord(ch) >= 0x2E80


def _draw_text(draw, o, size, cfg, missing):
    main = _font(o["face"], o["height"], o["bold"], cfg)
    if not main["found"]:
        missing.add(o["face"])
    fallback = main if main["cjk"] else _font(CJK_FALLBACK, o["height"], o["bold"], cfg)
    for i, line in enumerate(_with_size(o["text"], size).replace("\r", "").split("\n")):
        x, y = o["x"], o["y"] + i * o["height"]
        start = 0
        while start < len(line):  # 把一行切成「同一種字型」的片段
            cjk = _is_cjk(line[start])
            end = start + 1
            while end < len(line) and _is_cjk(line[end]) == cjk:
                end += 1
            f = fallback if cjk else main
            run = line[start:end]
            for dx in ((0, 1) if f["fake_bold"] else (0,)):
                draw.text((x + dx, y + f["ascent"]), run, font=f["font"], fill=0, anchor="ls")
            x += f["font"].getlength(run)
            start = end


# ---------- 繪製 ----------

def _graphic(o):
    img = Image.open(io.BytesIO(o["bmp"])).convert("L")
    if o["w"] > 0 and o["h"] > 0 and (o["w"], o["h"]) != img.size:
        img = img.resize((o["w"], o["h"]), Image.BOX)
    # 縮小後的灰階再轉回黑白：門檻偏亮，細線才不會消失
    return img.point(lambda v: 0 if v < 170 else 255, mode="1")


def content_bottom(tpl):
    """內容最下緣的 y（點）；只算落在標籤寬度內的物件。"""
    limit = mm_to_dots(tpl["width_mm"], 203)
    bottom = 0
    for o in tpl["objects"]:
        if o["x"] >= limit:
            continue
        if o["type"] == "text":
            lines = o["text"].replace("\r", "").rstrip("\n").split("\n")
            while lines and not lines[-1].strip():
                lines.pop()
            bottom = max(bottom, o["y"] + len(lines) * o["height"])
        else:
            bottom = max(bottom, o["y"] + o["h"])
    return bottom


def label_height_mm(tpl, dpi):
    """列印用的標籤長度：布標檔設定的長度；內容比它長時加長到放得下（不截掉最後一行）。"""
    need = content_bottom(tpl) * 25.4 / dpi + 1
    return max(tpl["height_mm"], int(need) + (need % 1 > 0))


def render(tpl, cfg, size=None):
    """回傳 (mode '1' 的 PIL 圖, 警告訊息清單)。圖寬 = 設定的標籤寬，高 = label_height_mm。
    size 是要印在「尺寸:」後面的文字，None 表示維持布標原樣。"""
    dpi = cfg["printer"]["dpi"]
    W = mm_to_dots(cfg["label"]["width_mm"], dpi)
    H = mm_to_dots(label_height_mm(tpl, dpi), dpi)
    canvas = Image.new("1", (W, H), 1)
    draw = ImageDraw.Draw(canvas)
    draw.fontmode = "1"  # 不做灰階平滑，小字在 203dpi 才清楚
    missing = set()
    for o in tpl["objects"]:
        if o["type"] == "graphic":
            canvas.paste(_graphic(o), (o["x"], o["y"]))
        elif o["type"] == "line":
            draw.rectangle([o["x"], o["y"], o["x"] + o["w"] - 1, o["y"] + o["h"] - 1], fill=0)
        elif o["type"] == "rect":
            draw.rectangle([o["x"], o["y"], o["x"] + o["w"] - 1, o["y"] + o["h"] - 1], outline=0, width=o["thick"])
    for o in tpl["objects"]:  # 文字最後畫，壓在圖上面
        if o["type"] == "text":
            _draw_text(draw, o, size, cfg, missing)
    warnings = []
    if missing:
        warnings.append(f"這台電腦沒有字型「{'、'.join(sorted(missing))}」，改用替代字型，字形會和 GoLabel 印的不同")
    if tpl["unsupported"]:
        warnings.append(f"布標裡有不支援的物件，不會印出：{'、'.join(sorted(set(tpl['unsupported'])))}")
    if size and size_line(tpl) is None:
        warnings.append("這個布標沒有「尺寸:」那一行，尺寸不會印出")
    return canvas, warnings
