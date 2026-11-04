"""银行流水（对账单）解析与付款匹配。

要解决的问题
------------
系统里的「供应商付款」是**人手工录的**。人手核对一遍 89 笔流水既累又容易漏——
漏记的付款不会出现在台账和毛利里，直到对账时才被发现。

这里的做法：上传银行导出的对账单（Excel/CSV/PDF 文字层），自动与已有付款记录
匹配。匹配上的确认「这笔确实付过」，匹配不上的单独列出，提示可能漏记。

匹配算法
--------
按「金额 + 日期 + 收款方/摘要」三档打分，不用一条非黑即白的规则：

    金额完全相同（±0.01）        是必要条件——钱不会对半分着付
    日期相同                      强信号
    日期相差 3 天内               中等信号（银行记账日与实际转账日常差 1-2 天）
    收款方名称与供应商名匹配      强信号（复用了 bank_parser.match_supplier 的思路）
    摘要命中备注关键词            弱信号

打分 ≥ 阈值才算「可能匹配」，其余一律列为待人工确认。**绝不自动创建付款记录**——
金额猜错比让人多看一眼的代价大得多。

支持的输入
----------
- .xlsx / .xls（openpyxl / xlrd）
- .csv（自动尝试 utf-8-sig / gbk / gb18030 编码）
- .pdf（走 pdfplumber 抽文字层，再按行解析；扫描件 PDF 无法解析，会明确告知）

各家银行的表头差别很大，所以表头识别用「关键词模糊匹配 + 列顺序兜底」，
识别不出来时让用户在页面上手动指定列。
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta

# ---- 表头关键词 ----
COL_KEYS = {
    "date": ["交易日期", "交易日", "记账日期", "交易时间", "入账日期", "发生日期",
             "发生时间", "日期", "time", "date", "交易明细日期", "记账日"],
    "amount": ["交易金额", "发生额", "金额", "交易额", "amount"],
    "debit": ["借方发生额", "借方金额", "支出金额", "付款金额", "转出金额", "支出"],
    "credit": ["贷方发生额", "贷方金额", "收入金额", "收款金额", "转入金额", "收入"],
    "direction": ["借贷标志", "借贷方向", "收付标志", "资金方向", "借或贷", "收/付"],
    "payee": ["对方户名", "收款人名称", "收款方名称", "对方名称", "收款人",
              "对方账户名", "交易对手", "户名", "payee", "对手户名"],
    "payee_account": ["对方账号", "收款账号", "对方账户", "收款方账号", "账号"],
    "summary": ["摘要", "附言", "用途", "备注", "交易摘要", "说明", "摘要说明",
                "summary", "remark", "comment"],
    "balance": ["余额", "账户余额", "balance"],
    "own_account": ["本方账号", "交易账号", "我方账号", "账号余额"],
}

# 支出方向标志：这些值代表「钱出去了」
OUTWARD = {"支出", "付", "转出", "借", "借方", "d", "debit", "dr", "出", "付款"}
# 收入方向标志
INWARD = {"收入", "收", "转入", "贷", "贷方", "c", "credit", "cr", "入", "收款"}

_SKIP_ROWS = re.compile(
    r"^(合计|小计|总计|本页合计|累计|账户余额|页次|第\s*\d+\s*页|交易明细|打印|制表)")


def _norm(v) -> str:
    return str(v or "").strip().replace("　", "").replace("\xa0", "")


def _to_float(v) -> float | None:
    """金额转 float。处理千分位逗号、全角符号、以及括号表示的负数。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _norm(v)
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in ("-", "."):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


def _to_date(v):
    """各种日期写法 → date。支持 Excel 序列号、datetime、字符串。"""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    # Excel 序列号（1900 日期系统）
    if isinstance(v, (int, float)) and 20000 < float(v) < 60000:
        try:
            return (datetime(1899, 12, 30) + timedelta(days=float(v))).date()
        except (ValueError, OverflowError):
            return None
    s = _norm(v)
    m = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"^(\d{1,2})\s*[-/月.]\s*(\d{1,2})", s)
    if m:  # 只有月日，按当年补
        try:
            return date(date.today().year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _detect_header(rows: list[list]) -> int | None:
    """在前 12 行里找表头行：命中两个以上关键列即认为是表头。"""
    for i, row in enumerate(rows[:12]):
        cells = [_norm(c) for c in row]
        hits = 0
        for field, keys in COL_KEYS.items():
            for c in cells:
                if not c:
                    continue
                low = c.lower()
                if any(k.lower() in low or low in k.lower() for k in keys):
                    hits += 1
                    break
        if hits >= 3:
            return i
    return None


def _map_columns(header: list, first_data_row: list | None = None) -> dict:
    """表头 → 字段映射。命中优先，否则按位置兜底。

    兜底的坑：不能直接认定第 0 列是日期——很多流水第 0 列是「序号」，值是 1/2/3，
    当成日期会解析失败，于是整行日期为 None，日期匹配能力静默失效（只剩金额硬碰）。
    因此兜底时逐列试解析，取第一个真能解出日期的列。
    """
    cells = [_norm(c).lower() for c in header]
    mapping = {}
    for field, keys in COL_KEYS.items():
        for i, c in enumerate(cells):
            if not c:
                continue
            if any(k.lower() in c or c in k.lower() for k in keys):
                mapping[field] = i
                break
    # 兜底日期列：优先看数据行里哪一列真能解析成日期
    if "date" not in mapping and first_data_row:
        for i, v in enumerate(first_data_row):
            if _to_date(v) is not None:
                mapping["date"] = i
                break
    return mapping


def _rows_from_xlsx(data: bytes) -> list[list]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    rows = [[c for c in row] for row in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


def _rows_from_xls(data: bytes) -> list[list]:
    import xlrd
    book = xlrd.open_workbook(file_contents=data)
    sh = book.sheet_by_index(0)
    return [[sh.cell_value(r, c) for c in range(sh.ncols)]
            for r in range(sh.nrows)]


def _rows_from_csv(data: bytes) -> list[list]:
    text = None
    for enc in ("utf-8-sig", "gbk", "gb18030", "utf-16", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        return []
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _rows_from_pdf(data: bytes) -> list[list]:
    import pdfplumber
    rows = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                if line.strip():
                    rows.append(line.split())
    return rows


def parse_statement(data: bytes, filename: str = "") -> tuple[list[dict], str]:
    """解析银行流水文件。

    返回 (流水条目列表, 错误说明)。成功时错误说明为空串。
    每条：{date, amount, direction, payee, payee_account, summary, balance, raw}
    """
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    try:
        if ext in ("xlsx", "xlsm"):
            rows = _rows_from_xlsx(data)
        elif ext == "xls":
            rows = _rows_from_xls(data)
        elif ext == "csv":
            rows = _rows_from_csv(data)
        elif ext == "pdf":
            rows = _rows_from_pdf(data)
        else:
            return [], ("不支持的文件类型：%s。请上传银行导出的 Excel(.xlsx/.xls) 或 CSV；"
                        "PDF 仅支持带文字层的对账单（扫描件请改用 Excel）。" % ext)
    except Exception as e:
        return [], f"文件解析失败：{e}"

    if not rows:
        return [], "文件里没有读到任何数据行。"

    hi = _detect_header(rows)
    if hi is None:
        return [], ("没能识别出表头。请确认文件是银行导出的对账单（需含日期、金额、"
                    "对方户名等列），或改用 Excel 格式。")
    mapping = _map_columns(rows[hi], rows[hi + 1] if len(rows) > hi + 1 else None)

    if "date" not in mapping and "amount" not in mapping:
        return [], "识别到表头，但找不到「日期」或「金额」列，无法解析。"

    out = []
    for row in rows[hi + 1:]:
        if not row or not any(_norm(c) for c in row):
            continue
        first = _norm(row[0])
        if first and _SKIP_ROWS.match(first):
            continue

        def cell(field):
            i = mapping.get(field)
            if i is None or i >= len(row):
                return None
            return row[i]

        d = _to_date(cell("date"))
        amt = _to_float(cell("amount"))
        debit = _to_float(cell("debit"))
        credit = _to_float(cell("credit"))

        # 银行导出常见两种形态：
        #   a) 单一「交易金额」列 + 借贷标志（或靠正负号）
        #   b) 「借方发生额」/「贷方发生额」双列，支出填借方、收入填贷方
        flow = None
        amount = None
        if debit is not None and abs(debit) > 0:
            flow, amount = "out", abs(debit)
        elif credit is not None and abs(credit) > 0:
            flow, amount = "in", abs(credit)
        elif amt is not None and amt != 0:
            dirn = _norm(cell("direction")).lower()
            if dirn in OUTWARD:
                flow = "out"
            elif dirn in INWARD:
                flow = "in"
            else:
                flow = "out" if amt < 0 else "in"
            amount = abs(amt)

        if d is None and amount is None:
            continue          # 既无日期又无金额 → 不是数据行
        if amount is None:
            continue          # 没有金额的行（如表尾、标题行）跳过

        out.append({
            "date": d, "amount": round(amount, 2), "direction": flow,
            "payee": _norm(cell("payee")), "payee_account": _norm(cell("payee_account")),
            "summary": _norm(cell("summary")), "balance": _to_float(cell("balance")),
            "own_account": _norm(cell("own_account")),
            "raw": " | ".join(_norm(c) for c in row[:8]),
        })

    if not out:
        return [], "解析到表头，但没有任何有效流水行（可能全是合计行，或日期/金额格式无法识别）。"
    return out, ""


# ---------------------------------------------------------------- 匹配

# 打分阈值：低于此分不作为候选
MATCH_THRESHOLD = 40


def _supplier_key(name: str) -> str:
    """供应商名归一：去掉公司后缀、空格、括号，用于宽松比对。"""
    s = _norm(name)
    for suffix in ("有限公司", "有限责任公司", "股份有限公司", "分公司", "公司"):
        s = s.replace(suffix, "")
    return s.replace(" ", "")


def match_payments(db, items: list[dict], tolerance: float = 0.01,
                   day_window: int = 3) -> dict:
    """把流水与已有付款记录匹配。

    返回：
      {
        "matched":  [{item, payment, score, reasons}],
        "unmatched_items": [item],          # 流水里没找到对应付款 → 可能漏记
        "unmatched_payments": [payment],    # 系统里有付款但流水里没有 → 可能漏录凭证
        "stats": {...}
      }

    只做匹配与提示，不修改任何数据。确认后由用户在界面上决定要不要补录。
    """
    # 作废的付款不参与对账：它会被 `used` 集合「消费」掉一笔真实流水，
    # 结果是真正付款的那条被判成「漏录凭证」——错的方向正好相反，
    # 比不作废更糟。
    from ..models import Supplier, SupplierPayment
    from ..voided import not_voided

    payments = db.query(SupplierPayment).filter(not_voided(SupplierPayment)).all()
    sup_names = {}
    for s in db.query(Supplier).all():
        sup_names[s.id] = s.name or ""

    used = set()          # 已被匹配走的 payment_id，防止一付款配多流水
    matched = []

    for it in items:
        if it["direction"] != "out":
            continue        # 只对支出流水做匹配（收入是客户回款，不在付款对账范围）
        best = None
        for p in payments:
            if p.id in used:
                continue
            amt = float(p.amount or 0)
            # 金额比对先四舍五入到分，避免 34258.41-34258.40 这类浮点误差
            # 把「正好差 1 分」判成「差 1 分以上」而漏掉本该匹配上的记录。
            if abs(round(amt - it["amount"], 2)) > tolerance:
                continue    # 金额不符直接跳过，这是硬条件
            score = 0
            reasons = []
            # 金额一致（本就开始的基础分）
            score += 40
            reasons.append("金额一致")
            if p.pay_date and it["date"]:
                gap = abs((p.pay_date - it["date"]).days)
                if gap == 0:
                    score += 35
                    reasons.append("日期一致")
                elif gap <= day_window:
                    score += 22
                    reasons.append(f"日期相差 {gap} 天")
            # 收款方与供应商名比对
            it_key = _supplier_key(it["payee"])
            p_sup = _supplier_key(sup_names.get(p.supplier_id, ""))
            if it_key and p_sup:
                if it_key == p_sup:
                    score += 25
                    reasons.append("收款方与供应商完全一致")
                elif it_key in p_sup or p_sup in it_key:
                    score += 15
                    reasons.append("收款方与供应商名称相近")
            # 摘要命中付款备注
            sm = _norm(it["summary"])
            note = _norm(p.note)
            if sm and note:
                for token in re.split(r"[\s,，。;；/、]+", note):
                    if len(token) >= 2 and token in sm:
                        score += 12
                        reasons.append(f"摘要命中「{token}」")
                        break
            if score >= MATCH_THRESHOLD and (best is None or score > best[0]):
                best = (score, p, reasons)

        if best:
            score, p, reasons = best
            used.add(p.id)
            matched.append({"item": it, "payment": p, "score": score,
                            "reasons": reasons,
                            "supplier": sup_names.get(p.supplier_id, "")})

    unmatched_items = [it for it in items
                       if it["direction"] == "out" and not any(m["item"] is it for m in matched)]
    unmatched_payments = [p for p in payments if p.id not in used]

    return {
        "matched": matched,
        "unmatched_items": unmatched_items,
        "unmatched_payments": unmatched_payments,
        "stats": {
            "total": len(items),
            "out_count": sum(1 for i in items if i["direction"] == "out"),
            "in_count": sum(1 for i in items if i["direction"] == "in"),
            "matched": len(matched),
            "unmatched_items": len(unmatched_items),
            "unmatched_payments": len(unmatched_payments),
            "matched_amount": round(sum(m["item"]["amount"] for m in matched), 2),
            "unmatched_amount": round(sum(i["amount"] for i in unmatched_items), 2),
        },
    }