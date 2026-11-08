"""把一筆標籤資料畫成黑白點陣圖（預覽與列印共用同一張圖）。"""
import glob
import os

from PIL import Image, ImageDraw, ImageFont

import settings

# 依序嘗試的系統字型 (路徑, ttc index)。熱感印表機 203dpi 下，粗一點的字比細明體清楚。
FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\msjh.ttc", 0),        # 微軟正黑體
    (r"C:\Windows\Fonts\mingliu.ttc", 0),     # 細明體
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),  # 黑體-繁 中
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 7),  # 宋體-繁
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
]


def resolve_font(cfg):
    path = cfg["font"].get("path") or ""
    if path and os.path.exists(path):
        return path, int(cfg["font"].get("index") or 0)
    # 專案 fonts/ 資料夾內放的字型優先
    for p in sorted(glob.glob(os.path.join(settings.BASE_DIR, "fonts", "*"))):
        if p.lower().endswith((".ttf", ".otf", ".ttc")):
            return p, 0
    for p, idx in FONT_CANDIDATES:
        if os.path.exists(p):
            return p, idx
    raise RuntimeError("找不到中文字型，請在設定中指定字型檔路徑，或把 .ttf 放進 fonts/ 資料夾")


def mm_to_dots(mm, dpi):
    return int(round(mm * dpi / 25.4))


def label_lines(label, cfg):
    """回傳 [(前綴, 內容), ...]。前綴為空字串的行會對齊到上一個前綴之後（縮排續行）。"""
    company = cfg["company"]
    lines = [("型號:", label["型號"])]
    items = label["成分"] or [""]
    lines.append(("成分:", items[0]))
    for it in items[1:]:
        lines.append(("", it))
    if label.get("布料"):
        lines.append(("", label["布料"]))
    lines.append(("尺寸:", f'{label["寬"]} x {label["長"]} cm'))
    for key in ("製造廠商", "廠商地址", "統一編號", "服務專線"):
        lines.append((key + ":", company.get(key, "")))
    lines.append(("製造地:", company.get("製造地", "")))
    return lines


def _line_x(lines, font):
    """每行內容的起始 x：有前綴就緊接前綴；續行對齊上一個前綴。"""
    xs, indent = [], 0
    for prefix, _ in lines:
        if prefix:
            indent = font.getlength(prefix)
        xs.append(indent)
    return xs


def _fit_font(lines, font_path, font_index, max_px, box_w, box_h, line_gap=1.3):
    size = max_px
    while True:
        font = ImageFont.truetype(font_path, size, index=font_index)
        xs = _line_x(lines, font)
        widest = max(x + font.getlength(t) for x, (_, t) in zip(xs, lines))
        total_h = int(size * line_gap) * len(lines)
        if size <= 8 or (widest <= box_w and total_h <= box_h):
            return font, xs
        size -= 1


def _logo_image(logo_path, max_w, max_h):
    img = Image.open(logo_path)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, "white")
        bg.alpha_composite(img)
        img = bg
    img = img.convert("L")
    img.thumbnail((max_w, max_h), Image.LANCZOS)
    return img


def render(label, cfg):
    """回傳 mode '1' 的 PIL 圖（白底黑字），尺寸 = 標籤寬高 (dots)。"""
    dpi = cfg["printer"]["dpi"]
    lc = cfg["label"]
    W = mm_to_dots(lc["width_mm"], dpi)
    H = mm_to_dots(lc["height_mm"], dpi)
    margin = mm_to_dots(lc["margin_mm"], dpi)

    canvas = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(canvas)

    y = margin + mm_to_dots(lc["logo_top_mm"], dpi)
    logo_path = settings.find_logo(label.get("品牌Logo", "")) if label.get("品牌Logo") else None
    if logo_path:
        logo = _logo_image(logo_path, W - 2 * margin, int(H * lc["logo_height_ratio"]))
        canvas.paste(logo, ((W - logo.width) // 2, y))
        y += logo.height + mm_to_dots(2, dpi)

    font_path, font_index = resolve_font(cfg)
    lines = label_lines(label, cfg)
    max_px = mm_to_dots(lc["max_font_mm"], dpi)
    font, xs = _fit_font(lines, font_path, font_index, max_px, W - 2 * margin, H - margin - y)
    step = int(font.size * 1.3)
    for (prefix, text), x in zip(lines, xs):
        if prefix:
            draw.text((margin, y), prefix, font=font, fill=0)
        draw.text((margin + x, y), text, font=font, fill=0)
        y += step

    # 二值化（不抖動），文字邊緣才乾淨
    return canvas.point(lambda v: 0 if v < 128 else 255, mode="1")


def render_png(label, cfg, path_or_buf):
    render(label, cfg).save(path_or_buf, format="PNG")


SAMPLE_LABEL = {
    "型號": "懶骨窩",
    "品牌Logo": "HO-ZZZ",
    "成分": [
        "高碳鋼袋裝獨立筒",
        "天然乳膠層",
        "高密度釋壓棉",
        "高密度支撐泡棉",
        "封邊泡棉床緣底面強化",
        "天絲緹花床墊專用布",
    ],
    "布料": "Polyester 66%  Lyocell(天絲) 34%",
    "寬": 152,
    "長": 188,
    "數量": 1,
}
