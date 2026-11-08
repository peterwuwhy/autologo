"""讀取上傳的 Excel（工作表「型號資料」+「訂單」），驗證後回傳標籤清單。"""
from openpyxl import load_workbook

import settings

MODEL_SHEET = "型號資料"
ORDER_SHEET = "訂單"
MODEL_COLS = ["型號", "品牌Logo", "成分", "布料"]
ORDER_COLS = ["型號", "寬(cm)", "長(cm)", "數量", "備註"]


MAX_COPIES = 32767  # EZPL ^P 上限


def _text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    # 部分工具產生的 xlsx 會把換行存成 _x000D_
    return str(v).replace("_x000D_", "").strip()


def _header_map(ws, required, sheet_name, errors):
    header = [_text(c.value) for c in ws[1]]
    cols = {name: i for i, name in enumerate(header) if name}
    missing = [c for c in required if c not in cols]
    if missing:
        errors.append(f"工作表「{sheet_name}」第 1 列缺少欄位：{'、'.join(missing)}")
        return None
    return cols


def _positive_number(v):
    """回傳正數（整數優先），不合法回傳 None。"""
    try:
        n = float(_text(v))
    except ValueError:
        return None
    if n <= 0:
        return None
    return int(n) if n.is_integer() else n


def read(file):
    """file：路徑或檔案物件。回傳 {"labels": [...], "errors": [...]}。"""
    errors, labels = [], []
    try:
        wb = load_workbook(file, data_only=True, read_only=True)
    except Exception as e:  # 非 xlsx、檔案損毀
        return {"labels": [], "errors": [f"無法開啟 Excel 檔：{e}"]}

    for name in (MODEL_SHEET, ORDER_SHEET):
        if name not in wb.sheetnames:
            errors.append(f"找不到工作表「{name}」（目前有：{'、'.join(wb.sheetnames)}）")
    if errors:
        return {"labels": [], "errors": errors}

    # 型號資料
    ws = wb[MODEL_SHEET]
    cols = _header_map(ws, ["型號", "品牌Logo", "成分"], MODEL_SHEET, errors)
    models, bad_models = {}, set()
    if cols:
        for r, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            get = lambda c: _text(row[cols[c]]) if c in cols and cols[c] < len(row) else ""
            model = get("型號")
            if not model:
                continue
            where = f"型號資料第 {r} 列（{model}）"
            if model in models:
                errors.append(f"{where}：型號重複，以第一筆為準")
                continue
            logo = get("品牌Logo")
            n_err = len(errors)
            if not logo:
                errors.append(f"{where}：品牌Logo 空白")
            elif not settings.find_logo(logo):
                errors.append(f"{where}：找不到 logo 圖檔「{logo}」，請到設定上傳")
            items = [s.strip() for s in get("成分").splitlines() if s.strip()]
            if not items:
                errors.append(f"{where}：成分空白")
            models[model] = {"品牌Logo": logo, "成分": items, "布料": get("布料")}
            if len(errors) > n_err:
                bad_models.add(model)

    # 訂單
    ws = wb[ORDER_SHEET]
    cols = _header_map(ws, ["型號", "寬(cm)", "長(cm)", "數量"], ORDER_SHEET, errors)
    if cols:
        for r, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            get = lambda c: row[cols[c]] if c in cols and cols[c] < len(row) else None
            if all(_text(v) == "" for v in row):
                continue
            model = _text(get("型號"))
            where = f"訂單第 {r} 列"
            row_errors = []
            if not model:
                row_errors.append("型號空白")
            elif model not in models:
                row_errors.append(f"型號「{model}」不在型號資料中")
            elif model in bad_models:
                row_errors.append(f"型號「{model}」的型號資料有誤（見上方）")
            w, h = _positive_number(get("寬(cm)")), _positive_number(get("長(cm)"))
            if w is None:
                row_errors.append(f"寬「{_text(get('寬(cm)'))}」不是正數")
            if h is None:
                row_errors.append(f"長「{_text(get('長(cm)'))}」不是正數")
            qty = _positive_number(get("數量"))
            if qty is None or not isinstance(qty, int):
                row_errors.append(f"數量「{_text(get('數量'))}」不是正整數")
            elif qty > MAX_COPIES:
                row_errors.append(f"數量 {qty} 超過上限 {MAX_COPIES}")
            if row_errors:
                errors.append(f"{where}：" + "；".join(row_errors))
                continue
            labels.append({
                "列": r,
                "型號": model,
                **models[model],
                "寬": w,
                "長": h,
                "數量": qty,
                "備註": _text(get("備註")),
            })
    wb.close()
    return {"labels": labels, "errors": errors}
