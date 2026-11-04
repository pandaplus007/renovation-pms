"""Excel 材料清单解析工具。

支持常见装修材料清单/报价单表格，自动识别表头：
  材料名称 | 规格型号 | 单位 | 数量 | 单价
表头列名做了模糊匹配（如「材料名称」「名称」「材料」都识别为名称列），
前 10 行内自动定位表头行，并跳过合计/小计等汇总行。
"""
import re
from io import BytesIO

from openpyxl import load_workbook

NAME_KEYS = ["材料名称", "材料名", "名称", "材料", "品名", "产品名称", "物料名称"]
SPEC_KEYS = ["规格型号", "规格", "型号", "尺寸"]
BRAND_KEYS = ["品牌", "厂家", "厂商", "牌子"]
UNIT_KEYS = ["单位"]
QTY_KEYS = ["数量", "用量", "预算用量", "工程数量", "工程量", "需求数量", "采购数量"]
PRICE_KEYS = ["单价", "预算单价", "参考单价", "价格", "预算价格", "成交单价"]
NET_PRICE_KEYS = ["不含税单价", "未税单价", "不含税价格", "未税价格", "净价", "税前单价"]
TAX_RATE_KEYS = ["税率", "税率%"]
NOTE_KEYS = ["备注", "说明", "注释", "附注", "remark", "note"]
SKIP_TEXT = {"合计", "小计", "总计", "汇总", "合计金额"}
SUMMARY_KEYWORDS = ["合计", "小计", "总计", "汇总", "大写", "含税", "不含税", "金额总计", "税额合计", "总金额", "累计", "本页", "合计金额", "总计金额"]
BUDGET_CATEGORY_KEYS = ["科目", "类别", "分类", "费用科目", "预算科目"]
BUDGET_ITEM_KEYS = ["预算项", "项目名称", "费用项目", "项目", "名称", "内容", "明细"]
BUDGET_AMOUNT_KEYS = ["金额", "预算金额", "预算费用", "费用", "金额(元)", "价格"]


def _normalize(text) -> str:
    return re.sub(r"[\s（）()*＊\-—]+", "", str(text or "")).lower()


def _match_key(cell, keys) -> bool:
    text = _normalize(cell)
    return any(k in text for k in keys)


def _is_summary_row(name) -> bool:
    """判断是否为汇总/合计行（含合并单元格的「含税金额总计」等）。
    用 normalize 去空格/全角标点后匹配，兼容「合 计」「合计：」「总计金额」等写法。"""
    n = str(name or "").strip()
    if not n:
        return True
    nn = _normalize(n)
    return any(_normalize(k) in nn for k in SUMMARY_KEYWORDS)


def _find_header(ws):
    """在前 10 行内定位包含「名称」类列的表头行。"""
    for row in ws.iter_rows(min_row=1, max_row=10):
        if any(_match_key(c.value, NAME_KEYS) for c in row):
            return row
    return None


def _to_float(v):
    try:
        if v is None or v == "":
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).replace(",", "").replace("￥", "").replace("¥", "").strip()
        # 提取数字（兼容 "50个"、"50 米"、"约50"、"50.5m2" 等）
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group(0)) if m else None
    except (ValueError, TypeError):
        return None


def _to_tax_rate(v):
    """税率解析：支持 13 / '13%' / '13％' / 0.13（Excel 百分比格式存储为小数）。
    统一返回百分比数值（如 13 表示 13%）。"""
    if isinstance(v, bool) or v is None or v == "":
        return 0
    if isinstance(v, str):
        s = (v or "").strip().rstrip("%").rstrip("％").strip()
        try:
            f = float(s)
        except ValueError:
            return 0
    elif isinstance(v, (int, float)):
        f = float(v)
    else:
        return 0
    if 0 < f <= 1:
        # Excel 百分比格式单元格：0.13 = 13%
        f *= 100
    return round(f, 2)


def parse_budget_excel(data: bytes) -> list[dict]:
    """解析预算 Excel，返回 [{category, item_name, amount}]。

    表头：科目（类别/分类）| 预算项（项目/名称）| 金额（预算金额）。
    """
    wb = load_workbook(BytesIO(data), data_only=True)
    ws = wb.active
    header = None
    for row in ws.iter_rows(min_row=1, max_row=10):
        cells = [c.value for c in row]
        has_cat = any(_match_key(c, BUDGET_CATEGORY_KEYS) for c in cells)
        has_item = any(_match_key(c, BUDGET_ITEM_KEYS) for c in cells)
        has_amount = any(_match_key(c, BUDGET_AMOUNT_KEYS) for c in cells)
        if (has_cat or has_item) and has_amount:
            header = row
            break
    if header is None:
        raise ValueError("未找到预算表头，需包含「科目 / 预算项 / 金额」列")

    col_map = {}
    for idx, cell in enumerate(header):
        if _match_key(cell.value, BUDGET_CATEGORY_KEYS):
            col_map["category"] = idx
        elif _match_key(cell.value, BUDGET_ITEM_KEYS):
            col_map["item_name"] = idx
        elif _match_key(cell.value, BUDGET_AMOUNT_KEYS):
            col_map["amount"] = idx

    rows = []
    for row in ws.iter_rows(min_row=header[0].row + 1, values_only=True):
        def val(key):
            i = col_map.get(key)
            if i is None or i >= len(row):
                return None
            return row[i]
        amount = _to_float(val("amount"))
        if amount is None or amount <= 0:
            continue
        item = str(val("item_name") or "").strip()
        if not item:
            continue
        rows.append({
            "category": str(val("category") or "其他").strip() or "其他",
            "item_name": item,
            "amount": round(amount, 2),
        })
    if not rows:
        raise ValueError("未解析到有效预算数据，请确认金额列有数据")
    return rows


def parse_material_excel(data: bytes) -> list[dict]:
    """解析 Excel 材料清单，返回 [{name, spec, unit, quantity, price}]。"""
    wb = load_workbook(BytesIO(data), data_only=True)
    ws = wb.active
    header = _find_header(ws)
    if header is None:
        raise ValueError("未找到表头行，表格需包含「材料名称 / 名称」列")

    col_map = {}
    for idx, cell in enumerate(header):
        if _match_key(cell.value, NAME_KEYS):
            col_map["name"] = idx
        elif _match_key(cell.value, SPEC_KEYS):
            col_map["spec"] = idx
        elif _match_key(cell.value, BRAND_KEYS):
            col_map["brand"] = idx
        elif _match_key(cell.value, UNIT_KEYS):
            col_map["unit"] = idx
        elif _match_key(cell.value, QTY_KEYS):
            col_map["quantity"] = idx
        elif _match_key(cell.value, NET_PRICE_KEYS):
            col_map["net_price"] = idx
        elif _match_key(cell.value, TAX_RATE_KEYS):
            col_map["tax_rate"] = idx
        elif _match_key(cell.value, PRICE_KEYS):
            col_map["price"] = idx
        elif _match_key(cell.value, NOTE_KEYS):
            col_map["note"] = idx

    if "name" not in col_map:
        raise ValueError("表头未识别到「材料名称」列，请参考模板格式")

    rows = []
    for row in ws.iter_rows(min_row=header[0].row + 1, values_only=True):
        def val(key):
            i = col_map.get(key)
            if i is None or i >= len(row):
                return None
            return row[i]

        name = val("name")
        if name is None:
            continue
        name_str = str(name).strip()
        if _is_summary_row(name_str):
            continue
        quantity = _to_float(val("quantity"))
        if quantity is None or quantity <= 0:
            continue  # 无数量行视为无效/汇总行
        rows.append({
            "name": name_str,
            "spec": str(val("spec") or "").strip(),
            "brand": str(val("brand") or "").strip(),
            "unit": str(val("unit") or "件").strip() or "件",
            "quantity": round(quantity, 2),
            "price": round(_to_float(val("price")) or 0, 6),
            "net_price": round(_to_float(val("net_price")) or 0, 6),
            "tax_rate": _to_tax_rate(val("tax_rate")),
            "note": str(val("note") or "").strip(),
        })

    if not rows:
        raise ValueError("未解析到有效材料数据，请确认数量列有数据")
    return rows


def parse_material_xls(data: bytes) -> list[dict]:
    """解析老版 .xls 材料清单（xlrd）。"""
    import xlrd
    wb = xlrd.open_workbook(file_contents=data)
    ws = wb.sheet_by_index(0)
    header_row = None
    for r in range(min(ws.nrows, 10)):
        if any(_match_key(ws.cell_value(r, c), NAME_KEYS) for c in range(ws.ncols)):
            header_row = r
            break
    if header_row is None:
        raise ValueError("未找到表头行，表格需包含「材料名称 / 名称」列")
    col_map = {}
    for c in range(ws.ncols):
        v = ws.cell_value(header_row, c)
        if _match_key(v, NAME_KEYS):
            col_map["name"] = c
        elif _match_key(v, SPEC_KEYS):
            col_map["spec"] = c
        elif _match_key(v, BRAND_KEYS):
            col_map["brand"] = c
        elif _match_key(v, UNIT_KEYS):
            col_map["unit"] = c
        elif _match_key(v, QTY_KEYS):
            col_map["quantity"] = c
        elif _match_key(v, NET_PRICE_KEYS):
            col_map["net_price"] = c
        elif _match_key(v, TAX_RATE_KEYS):
            col_map["tax_rate"] = c
        elif _match_key(v, PRICE_KEYS):
            col_map["price"] = c
        elif _match_key(v, NOTE_KEYS):
            col_map["note"] = c
    if "name" not in col_map:
        raise ValueError("表头未识别到「材料名称」列，请参考模板格式")
    rows = []
    for r in range(header_row + 1, ws.nrows):
        def val(key):
            i = col_map.get(key)
            return ws.cell_value(r, i) if i is not None else None
        name = str(val("name") or "").strip()
        if _is_summary_row(name):
            continue
        quantity = _to_float(val("quantity"))
        if quantity is None or quantity <= 0:
            continue
        rows.append({
            "name": name,
            "spec": str(val("spec") or "").strip(),
            "brand": str(val("brand") or "").strip(),
            "unit": str(val("unit") or "件").strip() or "件",
            "quantity": round(quantity, 2),
            "price": round(_to_float(val("price")) or 0, 6),
            "net_price": round(_to_float(val("net_price")) or 0, 6),
            "tax_rate": _to_tax_rate(val("tax_rate")),
            "note": str(val("note") or "").strip(),
        })
    if not rows:
        raise ValueError("未解析到有效材料数据，请确认数量列有数据")
    return rows


def _parse_table_rows(header_cells, data_rows):
    """通用：根据表头单元格文本列表 + 数据行（每行为单元格文本列表）解析材料行。"""
    col_map = {}
    for idx, h in enumerate(header_cells):
        if _match_key(h, NAME_KEYS):
            col_map["name"] = idx
        elif _match_key(h, SPEC_KEYS):
            col_map["spec"] = idx
        elif _match_key(h, BRAND_KEYS):
            col_map["brand"] = idx
        elif _match_key(h, UNIT_KEYS):
            col_map["unit"] = idx
        elif _match_key(h, QTY_KEYS):
            col_map["quantity"] = idx
        elif _match_key(h, NET_PRICE_KEYS):
            col_map["net_price"] = idx
        elif _match_key(h, TAX_RATE_KEYS):
            col_map["tax_rate"] = idx
        elif _match_key(h, PRICE_KEYS):
            col_map["price"] = idx
        elif _match_key(h, NOTE_KEYS):
            col_map["note"] = idx
    if "name" not in col_map or "quantity" not in col_map:
        return None
    rows = []
    for cells in data_rows:
        def val(key):
            i = col_map.get(key)
            if i is None or i >= len(cells):
                return None
            return cells[i]
        name = str(val("name") or "").strip()
        if _is_summary_row(name):
            continue
        quantity = _to_float(val("quantity"))
        if quantity is None or quantity <= 0:
            continue
        rows.append({
            "name": name,
            "spec": str(val("spec") or "").strip(),
            "brand": str(val("brand") or "").strip(),
            "unit": str(val("unit") or "件").strip() or "件",
            "quantity": round(quantity, 2),
            "price": round(_to_float(val("price")) or 0, 6),
            "net_price": round(_to_float(val("net_price")) or 0, 6),
            "tax_rate": _to_tax_rate(val("tax_rate")),
            "note": str(val("note") or "").strip(),
        })
    return rows or None


def parse_material_docx(data: bytes) -> list[dict]:
    """解析 Word .docx 中的材料清单表格（支持多表格，自动定位含名称/数量的表）。"""
    from docx import Document
    doc = Document(BytesIO(data))
    if not doc.tables:
        raise ValueError("Word 文档中未找到表格")
    for table in doc.tables:
        for hi in range(min(len(table.rows), 5)):
            header_cells = [c.text.strip() for c in table.rows[hi].cells]
            data_rows = [[c.text.strip() for c in r.cells] for r in table.rows[hi + 1:]]
            result = _parse_table_rows(header_cells, data_rows)
            if result:
                return result
    raise ValueError("Word 表格未识别到材料清单，需包含「名称 / 数量」列")


def _doc_table_text(data: bytes) -> str:
    """老版 .doc 表格文本提取：复用合同解析的 .doc 提取，保留单元格/行结构。"""
    from .contract_parser import extract_doc_text
    try:
        return extract_doc_text(data)
    except Exception:
        return ""


def parse_material_doc(data: bytes) -> list[dict]:
    """解析老版 .doc 材料清单表格（尽力而为，失败请转存 .docx/.xlsx）。"""
    text = _doc_table_text(data)
    if not text.strip():
        raise ValueError("无法解析 .doc 内容，请另存为 .docx / .xlsx 再导入")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    header_idx = None
    for i, ln in enumerate(lines[:15]):
        cells = [c.strip() for c in ln.split("\t")]
        if len(cells) < 3:
            continue
        # 必须同时含「名称」与「数量」列，否则正文里提到“材料”的句子会被误当表头
        if (any(_match_key(c, NAME_KEYS) for c in cells)
                and any(_match_key(c, QTY_KEYS) for c in cells)):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("未在 .doc 中找到表头行（需同时含「名称」与「数量」列），请另存为 .docx / .xlsx")
    header_cells = [c.strip() for c in lines[header_idx].split("\t")]
    data_rows = [[c.strip() for c in ln.split("\t")] for ln in lines[header_idx + 1:]]
    result = _parse_table_rows(header_cells, data_rows)
    if not result:
        raise ValueError(".doc 表格未识别到材料清单，请另存为 .docx / .xlsx")
    return result


def parse_material_file(filename: str, data: bytes) -> list[dict]:
    """按文件扩展名分发解析：.xlsx/.xlsm/.xls/.docx/.doc。"""
    lower = (filename or "").lower()
    if lower.endswith((".xlsx", ".xlsm")):
        return parse_material_excel(data)
    if lower.endswith(".xls"):
        return parse_material_xls(data)
    if lower.endswith(".docx"):
        return parse_material_docx(data)
    if lower.endswith(".doc"):
        return parse_material_doc(data)
    raise ValueError("不支持的文件格式，请使用 .xlsx / .xls / .docx / .doc")
