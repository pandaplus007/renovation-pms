"""发票 PDF 识别：文本提取 + 规则解析 + 客户/供应商自动匹配。

支持文本型电子发票 PDF（增值税电子发票/专票/普票），
扫描件（图片型 PDF）无法提取文本，会提示用户手动录入。
"""
import io
import re
from datetime import date

import pdfplumber
from sqlalchemy.orm import Session

from ..models import Customer, Project, Supplier

# ---------- PDF 文本提取 ----------

def extract_pdf_text(data: bytes) -> str:
    """从 PDF 字节流提取全部文本。"""
    text = ""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text += page_text + "\n"
    return text


# ---------- 规则解析 ----------

def _num(value: str | None):
    if not value:
        return None
    try:
        return float(value.replace(",", "").strip())
    except ValueError:
        return None


def _find_names(text: str) -> list[str]:
    """按“名称”位置切分，提取公司名称（排除“项目名称”等表头）。

    兼容两种版式：分区块（购方/销方信息分开）与数电票同行双栏
    （购 名称：A 销 名称：B）。
    """
    names = []
    for m in re.finditer(r"(?:购|买|销|售)?\s*名称\s*[:：]?\s*", text):
        seg = text[m.end():m.end() + 200]
        end_pos = len(seg)
        for marker in [r"(?:购|买|销|售)?\s*名称\s*[:：]?", r"统一社会信用代码",
                       r"纳税人识别号", r"项目名称", r"规格型号", r"\n"]:
            mm = re.search(marker, seg)
            if mm and mm.start() < end_pos:
                end_pos = mm.start()
        name = seg[:end_pos].strip(" 　:：")
        if name and not any(x in name for x in ["项目名称", "规格型号", "单位",
                                                "数量", "单价", "金额", "税率"]):
            names.append(name)
    return names


def _extract_buyer_seller(text: str) -> tuple[str, str, str, str]:
    """提取购方/销方的名称与税号。返回 (购方名称, 购方税号, 销方名称, 销方税号)。"""
    taxno_pattern = r"(?:统一社会信用代码|纳税人识别号)\s*[:：]?\s*([0-9A-Za-z]{15,20})"

    def _find_pos(markers):
        for m in markers:
            p = text.find(m)
            if p != -1:
                # 排除“购方开户银行/销方开户银行”（票据底部信息，不是购方信息区）
                if text[p + len(m):p + len(m) + 2] == "开户":
                    continue
                return p
        return -1

    pos_b = _find_pos(["购买方信息", "购方信息", "购买方", "购方"])
    pos_s = _find_pos(["销售方信息", "销方信息", "销售方", "销方"])
    if pos_b != -1 and pos_s != -1 and pos_b < pos_s:
        buyer_names = _find_names(text[pos_b:pos_s])
        seller_names = _find_names(text[pos_s:])
        buyer_taxnos = re.findall(taxno_pattern, text[pos_b:pos_s])
        seller_taxnos = re.findall(taxno_pattern, text[pos_s:])
        return (buyer_names[0] if buyer_names else "",
                buyer_taxnos[0] if buyer_taxnos else "",
                seller_names[0] if seller_names else "",
                seller_taxnos[0] if seller_taxnos else "")

    # 无分区标记时按出现顺序：名称/税号第一次=购方，第二次=销方
    names = _find_names(text)
    taxnos = re.findall(taxno_pattern, text)
    return (names[0] if names else "",
            taxnos[0] if taxnos else "",
            names[1] if len(names) > 1 else "",
            taxnos[1] if len(taxnos) > 1 else "")


def parse_invoice_text(text: str) -> dict:
    """从发票文本中解析关键字段（购方/销方名称与税号单独识别）。"""
    # 压缩中文单字之间的空格（如“发 票 号 码”→“发票号码”），不影响英文/数字/金额
    text = re.sub(r"(?<=[一-鿿])[ \t]+(?=[一-鿿])", "", text)
    result = {
        "invoice_no": "", "amount": 0.0, "tax_amount": 0.0, "tax_rate": 0.0,
        "invoice_date": None, "invoice_type": "增值税普通发票",
        "buyer_name": "", "buyer_tax_no": "",
        "seller_name": "", "seller_tax_no": "",
        "title": "", "seller": "", "tax_no": "",
        "raw_text": text[:3000],
    }

    def _clean(n: str) -> str:
        # 公司名去掉内部空格（PDF 排版断行/间距），税号等数字不受影响
        return re.sub(r"\s+", "", n or "").strip()

    # 发票号码
    m = re.search(r"发票号码\s*[:：]?\s*([0-9]{8,20})", text)
    if m:
        result["invoice_no"] = m.group(1)

    # 开票日期
    m = re.search(r"开票日期\s*[:：]?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        try:
            result["invoice_date"] = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 税额：优先取“合计”行第二个金额，其次“税 额”标签后的直接值（兼容“税 额”空格）
    m = re.search(r"合\s*计\s*[￥¥]?\s*([\d,]+\.\d{1,2})\s*[￥¥]?\s*([\d,]+\.\d{1,2})", text)
    if m:
        result["tax_amount"] = _num(m.group(2)) or 0.0
    if not result["tax_amount"]:
        m = re.search(r"税\s*额\s*[￥¥]?\s*([\d,]+\.\d{1,2})", text)
        if m:
            result["tax_amount"] = _num(m.group(1)) or 0.0

    # 价税合计（优先小写金额，其次取最后一个金额）
    m = re.search(r"[（(]\s*小写\s*[）)]\s*[￥¥]?\s*([\d,]+\.\d{1,2})", text)
    if not m:
        amounts = re.findall(r"[￥¥]\s*([\d,]+\.\d{1,2})", text)
        m = re.search(r"([\d,]+\.\d{1,2})", amounts[-1]) if amounts else None
    if m:
        result["amount"] = _num(m.group(1)) or 0.0

    # 税率：优先直接标签/明细列，其次按 税额/(价税合计-税额) 推算
    m = re.search(r"税\s*率\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%", text)
    if m:
        result["tax_rate"] = float(m.group(1))
    else:
        m = re.search(r"\b(1[0-3]|[0-9])\s*%", text)
        if m:
            result["tax_rate"] = float(m.group(1))
    if not result["tax_rate"] and result["tax_amount"] and result["amount"] > result["tax_amount"]:
        base = result["amount"] - result["tax_amount"]
        if base > 0:
            result["tax_rate"] = round(result["tax_amount"] / base * 100, 2)

    # 购方/销方名称与税号（分区提取）
    buyer_name, buyer_tax_no, seller_name, seller_tax_no = _extract_buyer_seller(text)
    result.update({
        "buyer_name": buyer_name, "buyer_tax_no": buyer_tax_no,
        "seller_name": seller_name, "seller_tax_no": seller_tax_no,
        "title": buyer_name,      # 兼容字段（购方名称）
        "seller": seller_name,    # 兼容字段（销方名称）
        "tax_no": buyer_tax_no,   # 兼容字段（购方税号）
    })

    # 发票类型
    if "专用发票" in text:
        result["invoice_type"] = "增值税专用发票"
    elif "普通发票" in text:
        result["invoice_type"] = "增值税普通发票"
    elif "电子发票" in text:
        result["invoice_type"] = "电子发票"

    for _k in ("buyer_name", "seller_name", "seller", "title"):
        if result.get(_k):
            result[_k] = _clean(result[_k])
    return result


# ---------- 自动匹配 ----------

def match_customer_project(db: Session, title: str) -> tuple[Customer | None, Project | None]:
    """按购方名称匹配客户，并选出其最相关项目（优先施工中，其次最新）。"""
    if not title:
        return None, None
    title = re.sub(r"\s+", "", title).strip()
    customer = None
    for c in db.query(Customer).all():
        cn = re.sub(r"\s+", "", c.name or "")
        if title == cn or (cn and (title in cn or cn in title)):
            customer = c
            break
    if customer is None:
        return None, None
    projects = sorted(customer.projects,
                      key=lambda p: (0 if p.status == "施工中" else 1, -p.id))
    return customer, (projects[0] if projects else None)


def match_supplier(db: Session, seller: str) -> Supplier | None:
    """按销方名称匹配供应商（比较时忽略名称内部空格）。"""
    if not seller:
        return None
    key = re.sub(r"\s+", "", seller).strip()
    if not key:
        return None
    for s in db.query(Supplier).all():
        sn = re.sub(r"\s+", "", s.name or "")
        if key == sn or (sn and (key in sn or sn in key)):
            return s
    return None
