"""银行转账单识别：PDF 文本规则解析 + 图片视觉识别 + 供应商匹配。

- PDF 电子回单：pdfplumber 提取文本后用规则解析（离线免费，不消耗额度）
- 图片/截图：调用视觉模型，平台可切换（见 utils/vision_client.py）
- 识别失败不吞原因：统一返回 (结果, 错误说明)，让界面把真实原因展示出来，
  把“额度不足 / key 无效 / 超时 / 模型不存在”与“图片不清晰”区分开
"""
import io
import re
from datetime import date

import pdfplumber
from sqlalchemy.orm import Session

from ..models import Supplier
from .vision_client import IMAGE_MEDIA_TYPES, call_vision, sniff_kind

BANK_SYSTEM_PROMPT = (
    "你是银行转账回单识别助手。请识别图片中的银行转账信息，只返回 JSON，"
    "不要任何解释和 markdown 代码块。格式："
    '{"payee":收款方户名,"payee_account":收款方账号,"payee_bank":收款方开户行,'
    '"payer":付款方户名,"payer_account":付款方账号,"payer_bank":付款方开户行,'
    '"amount":金额数字,"trade_date":交易日期,"summary":摘要/附言/用途}。'
    "识别不到的字段填空字符串。"
)


def extract_pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = ""
        for page in pdf.pages:
            text += (page.extract_text() or "") + "\n"
    return text


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("￥", "").replace("¥", "").strip())
    except (ValueError, TypeError):
        return 0.0


def parse_bank_text(text: str) -> dict:
    """从银行电子回单文本解析字段（收款方/付款方/金额/日期/摘要）。"""
    result = {"payee": "", "payer": "", "amount": 0.0, "trade_date": None, "summary": "",
              "payee_account": "", "payer_account": "", "bank": ""}

    m = re.search(r"(?:收款方|收款户名|收款人|对方户名|收款账户名称)\s*[:：]?\s*([^\n]{2,60})", text)
    if m:
        result["payee"] = m.group(1).strip()
    m = re.search(r"(?:付款方|付款户名|付款人|付款账户名称)\s*[:：]?\s*([^\n]{2,60})", text)
    if m:
        result["payer"] = m.group(1).strip()
    m = re.search(r"(?:收款账号|收款账户号|收款账户|对方账号|入账账号)\s*[:：]?\s*([0-9\-\*]{6,40})", text)
    if m:
        result["payee_account"] = m.group(1).strip()
    m = re.search(r"(?:付款账号|付款账户号|付款账户|本方账号)\s*[:：]?\s*([0-9\-\*]{6,40})", text)
    if m:
        result["payer_account"] = m.group(1).strip()
    m = re.search(r"(?:开户银行|开户行|收款银行|收款开户行)\s*[:：]?\s*([^\n]{2,60})", text)
    if m:
        result["bank"] = m.group(1).strip()
    m = re.search(r"(?:交易金额|转账金额|金额|实付金额)\s*[:：]?[￥¥]?\s*([\d,]+\.\d{1,2})", text)
    if m:
        result["amount"] = _num(m.group(1))
    m = re.search(r"(?:交易日期|转账日期|交易时间)\s*[:：]?\s*(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})", text)
    if m:
        try:
            result["trade_date"] = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"(?:摘要|附言|用途|备注)\s*[:：]?\s*([^\n]{2,60})", text)
    if m:
        result["summary"] = m.group(1).strip()
    return result


def recognize_images(items: list[tuple[bytes, str]]) -> tuple[dict | None, str]:
    """识别一组回单图片（多页扫描件与单张图片共用）。items 为 (字节, media_type)。

    返回 (字段字典, 错误说明)。
    """
    parsed, err = call_vision(items, BANK_SYSTEM_PROMPT,
                              "请识别这张银行转账单的所有字段。", max_tokens=2048, timeout=90)
    if not parsed:
        return None, err

    result = {
        "payee": str(parsed.get("payee", "") or "").strip(),
        "payer": str(parsed.get("payer", "") or "").strip(),
        "payee_account": str(parsed.get("payee_account", "") or "").strip(),
        "payee_bank": str(parsed.get("payee_bank", "") or "").strip(),
        "payer_account": str(parsed.get("payer_account", "") or "").strip(),
        "payer_bank": str(parsed.get("payer_bank", "") or "").strip(),
        "bank": str(parsed.get("bank", "") or "").strip(),
        "amount": _num(parsed.get("amount", 0)),
        "trade_date": None,
        "summary": str(parsed.get("summary", "") or "").strip(),
    }
    d = str(parsed.get("trade_date", "") or "")
    mm = re.search(r"(\d{4})[-/年.]?(\d{1,2})[-/月.]?(\d{1,2})", d)
    if mm:
        try:
            result["trade_date"] = date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
        except ValueError:
            pass

    # 模型通了但一个字段都没读出来：属于“图片本身识别不到”，与配置问题区分开
    if not any([result["payee"], result["payer"], result["amount"], result["summary"]]):
        return None, ("模型未能从这张图片里读到转账字段（图片可能过小、模糊或不是转账回单）；"
                      "可换清晰原图重试，或手动填写。")
    return result, ""


def recognize_image(data: bytes, media_type: str = "image/png") -> tuple[dict | None, str]:
    """识别单张图片转账单。返回 (字段字典, 错误说明)。"""
    return recognize_images([(data, media_type)])


def recognize_bank_file(filename: str, data: bytes) -> tuple[dict | None, str]:
    """按真实文件类型识别转账单。返回 (结果, 错误说明)。

    PDF 先规则解析（离线免费）；无文字层则当扫描件渲染成图片走视觉；
    图片直接走视觉。类型按文件内容判断，不只看扩展名。
    """
    kind = sniff_kind(data, filename)

    if kind == "pdf":
        try:
            text = extract_pdf_text(data)
        except Exception as e:
            return None, (f"PDF 打不开（文件可能在上传时损坏或未传完）：{e}；"
                          "请重新上传，或改用图片格式。")
        if text.strip():
            return parse_bank_text(text), ""
        # 扫描件 / 图片型电子回单：没有文字层，渲染成图片交给视觉模型
        # （与扫描件合同同一条路径；规则解析此时无任何可提取内容）
        try:
            from .contract_parser import render_pdf_pages
            images = render_pdf_pages(data, max_pages=2)
        except Exception as e:
            return None, f"扫描件 PDF 渲染成图片失败：{e}"
        if images:
            return recognize_images([(img, "image/png") for img in images])
        return None, ("该 PDF 提取不到文字，也无法渲染页面（文件可能已损坏）；"
                      "请重新导出 PDF 或改用图片上传。")

    if kind in IMAGE_MEDIA_TYPES:
        return recognize_images([(data, IMAGE_MEDIA_TYPES[kind])])

    return None, ("无法识别的文件类型：请上传 PNG / JPG / WebP / BMP 图片或 PDF 电子回单"
                  "（若文件确实是图片，请重新另存为标准格式）")


def match_supplier(db: Session, payee: str) -> Supplier | None:
    """按收款方名称匹配供应商：先精确/包含匹配，再模糊匹配最接近的供应商。"""
    if not payee:
        return None
    payee = payee.strip()
    suppliers = db.query(Supplier).all()
    # 1. 精确 / 包含匹配
    for s in suppliers:
        if payee == s.name or (s.name and (payee in s.name or s.name in payee)):
            return s
    # 2. 模糊匹配：找相似度最高且超过阈值的供应商
    import difflib
    best, best_score = None, 0.0
    for s in suppliers:
        if not s.name:
            continue
        score = difflib.SequenceMatcher(None, payee, s.name).ratio()
        if score > best_score:
            best_score, best = score, s
    if best and best_score >= 0.55:
        return best
    return None
