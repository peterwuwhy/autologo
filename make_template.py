"""產生 Excel 範本 標籤範本.xlsx（含懶骨窩範例）。也可直接執行：python make_template.py"""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from excel_reader import MODEL_COLS, MODEL_SHEET, ORDER_COLS, ORDER_SHEET
from label_render import SAMPLE_LABEL

HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")


def _header(ws, cols, widths):
    ws.append(cols)
    for i, w in enumerate(widths, start=1):
        cell = ws.cell(row=1, column=i)
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        ws.column_dimensions[cell.column_letter].width = w
    ws.freeze_panes = "A2"


def build_workbook():
    wb = Workbook()
    ws = wb.active
    ws.title = MODEL_SHEET
    _header(ws, MODEL_COLS, [14, 14, 30, 36])
    ws.append([
        SAMPLE_LABEL["型號"],
        SAMPLE_LABEL["品牌Logo"],
        "\n".join(SAMPLE_LABEL["成分"]),
        SAMPLE_LABEL["布料"],
    ])
    for c in ws[2]:
        c.alignment = Alignment(wrap_text=True, vertical="top")

    ws = wb.create_sheet(ORDER_SHEET)
    _header(ws, ORDER_COLS, [14, 10, 10, 8, 24])
    ws.append(["懶骨窩", 152, 188, 3, ""])
    ws.append(["懶骨窩", 182, 188, 2, "雙人加大"])

    ws = wb.create_sheet("說明")
    ws.column_dimensions["A"].width = 90
    for line in [
        "【型號資料】每個型號一列，設定一次即可。",
        "  型號：印在標籤上的型號名稱，必須唯一。",
        "  品牌Logo：logo 圖檔名稱（不含副檔名），圖檔請在程式「設定」頁上傳。",
        "  成分：一項一行，儲存格內按 Alt+Enter（Mac：Option+Enter）換行。",
        "  布料：布料成分，印在成分最後一行（可空白）。",
        "【訂單】每種規格一列，數量 = 要印幾張。",
        "  型號必須存在於型號資料；寬、長填公分數字；備註不會印出。",
        "請勿修改工作表名稱與第 1 列欄位名稱。",
    ]:
        ws.append([line])
    wb.active = 1
    return wb


def template_bytes():
    buf = io.BytesIO()
    build_workbook().save(buf)
    return buf.getvalue()


if __name__ == "__main__":
    build_workbook().save("標籤範本.xlsx")
    print("已產生 標籤範本.xlsx")
