"""采购合同文件识别：PDF/Word 文本提取 + 规则解析 + 扫描件视觉识别。

- 文本型 PDF / Word：pdfplumber / python-docx 提取文本后规则解析（离线免费）
- 扫描件（图片型 PDF）：渲染成图片后调用视觉模型，平台可切换（见 utils/vision_client.py）
- 识别失败统一返回 (结果, 错误说明)，把额度/key/超时等配置问题与“文件不清晰”区分开
"""
import io
import re
from datetime import date

import pdfplumber
from docx import Document as DocxDocument
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph

from .vision_client import IMAGE_MEDIA_TYPES, call_vision, sniff_kind

CONTRACT_SYSTEM_PROMPT = (
    "你是采购合同识别助手。识别图片中的合同关键信息，只返回 JSON，不要解释和代码块。"
    '格式：{"contract_no":合同编号,"name":合同名称,"party_a":甲方,"party_b":乙方,'
    '"amount":合同金额数字,"sign_date":签订日期(YYYY-MM-DD),'
    '"milestones":[{"name":节点名称,"m_date":日期(YYYY-MM-DD)}],'
    '"summary":合同内容概述}。识别不到的字段填空字符串或空数组。'
)

MILESTONE_KEYWORDS = [
    "首付款", "首付", "进度款", "尾款", "付款", "支付",
    "交货", "到货", "供货", "交付", "进场",
    "验收", "完工", "竣工", "安装完成", "质保",
]


def extract_text(filename: str, data: bytes) -> str:
    """按文件类型提取文本。"""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    if lower.endswith(".docx"):
        doc = DocxDocument(io.BytesIO(data))
        parts = []
        # 按正文真实顺序输出段落与表格（表格常插在条款之间；若整体挪到末尾，
        # 会破坏金额/付款等规则的先后匹配，如“含税金额总计”晚于付款条款出现）
        for block in _iter_docx_blocks(doc):
            if isinstance(block, DocxParagraph):
                parts.append(block.text or "")
            else:
                for row in block.rows:
                    # 单元格内部换行会拆断表格行导致材料清单列错位，统一压成空格
                    parts.append("\t".join(re.sub(r"\s*\n\s*", " ", c.text).strip() for c in row.cells))
        return "\n".join(parts)
    if lower.endswith(".doc"):
        return extract_doc_text(data)
    return ""


def extract_doc_text(data: bytes) -> str:
    """老版 Word（.doc，OLE2 复合文档）文本提取。

    .doc 没有可用的纯 Python 解析库，这里直接扫描 WordDocument 流的 UTF-16LE 正文，
    并把 Word 的控制符还原成可解析的结构：
      · 0x07（单元格/行结束）→ 制表符；行结束时表现为连续两个制表符，再统一换成换行
      · 0x0D / 0x0A / 0x0B / 0x0C（段落、软换行、分页）→ 换行
      · 其它不可打印字节直接丢弃，避免二进制元数据混进正文
    这样合同里的表格（如材料清单）能保留行列结构，而不是被压成一串无法解析的碎片。
    """
    try:
        import olefile
    except ImportError as e:
        # 服务器没装 olefile 时给出明确原因，而不是静默返回空文本、
        # 让用户误以为是“扫描件/文件不清晰”
        raise RuntimeError(
            "服务器缺少老版 Word（.doc）解析组件 olefile：请在服务器安装后重试，"
            "或把合同另存为 .docx / PDF 再上传"
        ) from e

    if not olefile.isOleFile(io.BytesIO(data)):
        return ""
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
        wd = ole.openstream("WordDocument").read()
    except Exception:
        return ""

    text_a = _scan_doc_stream(wd, 0)
    text_b = _scan_doc_stream(wd, 1)
    text = text_a if len(text_a) >= len(text_b) else text_b
    return _normalize_doc_text(text)


# .doc 正文可能出现的符号区（CJK 兼容、带圈数字、㎡ 等度量单位、常用标点），
# 这些双字节落在下面区间，若当二进制丢弃会连带拆断表格单元格。
_DOC_SYMBOL_RANGES = (
    (0x2000, 0x206F),   # 通用标点（— – ‘ ’ “ ” …）
    (0x2070, 0x209F),   # 上/下标
    (0x20A0, 0x20BF),   # 货币符号
    (0x2100, 0x214F),   # 字母式符号（№ ™ ℉）
    (0x2190, 0x21FF),   # 箭头
    (0x2460, 0x24FF),   # 带圈数字 ① ②
    (0x2500, 0x257F),   # 框线
    (0x25A0, 0x25FF),   # 几何图形 ■ □ ●
    (0x2600, 0x26FF),   # 杂项符号
    (0x3300, 0x33FF),   # CJK 兼容（㎡ ㍿ 等）
    (0xFE30, 0xFE4F),   # CJK 兼容形式
    (0xFF00, 0xFFEF),   # 全角字符
)


def _doc_char_ok(c: int) -> bool:
    """UTF-16 码位是否属于可读正文（ASCII、汉字、全角、常用符号、制表符）。"""
    if c == 0x09:
        return True
    if (0x20 <= c < 0x7F) or (0x3000 <= c <= 0x303F) or (0x4E00 <= c <= 0x9FFF) \
            or (0xF900 <= c <= 0xFAFF):
        return True
    return any(lo <= c <= hi for lo, hi in _DOC_SYMBOL_RANGES)


def _scan_doc_stream(wd: bytes, offset: int) -> str:
    """按 UTF-16LE 扫描 WordDocument 流，保留 Word 控制符所表达的结构。"""
    out = []
    for i in range(offset, len(wd) - 1, 2):
        c = wd[i] | (wd[i + 1] << 8)
        if c == 0x07:
            out.append("\t")
        elif c in (0x0A, 0x0B, 0x0C, 0x0D):
            out.append("\n")
        elif _doc_char_ok(c):
            out.append(chr(c))
    return "".join(out)


def _normalize_doc_text(text: str) -> str:
    """规整 .doc 扫描文本：连续制表符（单元格结束 + 行结束）还原为换行，并去掉二进制噪声。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\t{2,}", "\n", text)
    text = re.sub(r"[ \u3000]{2,}", " ", text)
    lines = []
    for line in text.split("\n"):
        line = line.rstrip("\t ")
        core = line.strip()
        if not core:
            continue
        # 既没有汉字、也没有 2 位以上字母数字的行，视为二进制噪声丢弃
        if not any("\u4e00" <= ch <= "\u9fff" for ch in core) and not re.search(r"[A-Za-z0-9]{2,}", core):
            continue
        lines.append(line)
    return "\n".join(lines)


def render_pdf_pages(data: bytes, max_pages: int = 3, resolution: int = 150) -> list[bytes]:
    """把 PDF 前几页渲染为 PNG 字节列表（供扫描件视觉识别）。"""
    images = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:max_pages]:
            img = page.to_image(resolution=resolution).original
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            images.append(buf.getvalue())
    return images


def _to_date(v) -> date | None:
    if not v:
        return None
    m = re.search(r"(\d{4})[-/年.]?(\d{1,2})[-/月.]?(\d{1,2})", str(v))
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def recognize_contract_vision(images: list[bytes]) -> tuple[dict | None, str]:
    """识别扫描件合同（PNG 字节列表）。返回 (字段字典, 错误说明)。"""
    return recognize_contract_vision_images([(img, "image/png") for img in images])


def recognize_contract_vision_images(items: list[tuple[bytes, str]]) -> tuple[dict | None, str]:
    """识别扫描件合同（(字节, media_type) 列表）。返回 (字段字典, 错误说明)。"""
    if not items:
        return None, "没有可识别的合同页面图片"

    parsed, err = call_vision(items, CONTRACT_SYSTEM_PROMPT,
                              "识别这份采购合同的关键信息", max_tokens=2048, timeout=120)
    if not parsed:
        return None, err

    milestones = []
    for ms in parsed.get("milestones", []) or []:
        if not isinstance(ms, dict):
            continue
        d = _to_date(ms.get("m_date"))
        if d and (ms.get("name") or "").strip():
            milestones.append({"name": str(ms["name"]).strip(), "m_date": d, "amount": 0.0})
    try:
        amount = float(str(parsed.get("amount", 0) or 0).replace(",", "") or 0)
    except (ValueError, TypeError):
        amount = 0.0
    result = {
        "contract_no": str(parsed.get("contract_no", "") or "").strip(),
        "name": str(parsed.get("name", "") or "").strip(),
        "party_a": str(parsed.get("party_a", "") or "").strip(),
        "party_b": str(parsed.get("party_b", "") or "").strip(),
        "amount": amount,
        "sign_date": _to_date(parsed.get("sign_date")),
        "milestones": milestones,
        "raw_text": str(parsed.get("summary", "") or "")[:1000],
    }
    if not any([result["contract_no"], result["name"], result["party_a"],
                result["party_b"], result["amount"]]):
        return None, ("模型未能从这份扫描件里读到合同字段（图片可能过小或模糊）；"
                      "可尝试提高扫描分辨率后重试，或手动填写。")
    return result, ""


def recognize_contract_file(filename: str, data: bytes) -> tuple[dict | None, str]:
    """统一识别入口：文本型走规则解析，扫描件/图片走视觉识别。返回 (结果, 错误说明)。

    先按文件内容判断真实类型，避免扩展名与内容不符（如扫描件存成 .pdf、
    图片存成 .pdf）时误进 PDF 文本提取分支而直接报错。
    """
    kind = sniff_kind(data, filename)

    # 图片型合同/扫描件：直接走视觉，不必先尝试文本提取
    if kind in IMAGE_MEDIA_TYPES:
        return recognize_contract_vision_images([(data, IMAGE_MEDIA_TYPES[kind])])

    try:
        text = extract_text(filename, data)
    except Exception as e:
        return None, f"文件读取失败：{e}"
    if text.strip():
        return parse_contract_text(text), ""
    if kind == "pdf":
        try:
            images = render_pdf_pages(data)
        except Exception as e:
            return None, (f"PDF 打不开或无法渲染（文件可能在上传时损坏或未传完）：{e}；"
                          "请重新上传，或改用图片格式。")
        if images:
            return recognize_contract_vision(images)
        return None, "该 PDF 没有可提取的文字，也无法渲染页面（文件可能已损坏）"
    return None, ("该文件提取不到文字内容（可能是扫描件或缺文字层的文档）："
                  "PDF 扫描件可尝试重新导出，Word 请确认未加密，或手动填写。")


def _parse_date_in(text: str, pos: int, window: int = 80) -> date | None:
    seg = text[pos:pos + window]
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", seg)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", seg)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def _iter_docx_blocks(doc: DocxDocument):
    """按 Word 正文真实顺序迭代段落（Paragraph）与表格（Table）。"""
    for child in doc.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield DocxParagraph(child, doc)
        elif isinstance(child, CT_Tbl):
            yield DocxTable(child, doc)


def _match_amount(keys: list[str], text: str, allow_nl: bool = False) -> float | None:
    """按金额汇总词取第一个金额；跳过“数字后紧跟 % / 天数”的候选。
    allow_nl=True 时允许跨至多 1 个换行（如“暂定合同总价：\n20585500.00”）。"""
    gap = r"[^\n]{0,24}?"
    pat = re.compile(
        r"(?:" + "|".join(
            r"(?<!税前)暂估总价" if k == "暂估总价" else re.escape(k) for k in keys
        ) + r")\s*[:：]?\s*" + gap + r"(\d[\d,]*)(?:\.(\d{1,2}))?"
    )
    for m in pat.finditer(text):
        after = re.sub(r"[\s，,；;。、/\\]+", "", text[m.end():m.end() + 3])[:1]
        if after in ("%", "天", "日"):
            continue
        try:
            num = m.group(1).replace(",", "")
            if m.group(2):
                num += "." + m.group(2)
            return float(num)
        except (ValueError, TypeError):
            continue
    return None


def parse_contract_text(text: str) -> dict:
    """从合同文本解析字段与时间节点。"""
    # 归一化：老版 .doc 提取的文本常以 \r 作换行，统一为 \n 保证行级正则可匹配
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    result = {
        "contract_no": "", "name": "", "party_a": "", "party_b": "",
        "project_name": "", "project_address": "",
        "amount": 0.0, "sign_date": None,
        "start_date": None, "end_date": None,
        "duration_days": 0, "pay_method": "", "tax_rate": 0.0,
        "prepay_rate": 0.0, "warranty_months": 0, "warranty_retention_rate": 0.0,
        "pay_terms": [],
        "a_contact": "", "b_contact": "",
        "a_tax_no": "", "b_tax_no": "",
        "a_bank": "", "a_account": "",
        "b_bank": "", "b_account": "",
        "milestones": [],
        "materials": [],
        "raw_text": text[:4000],
    }

    # 工程名称 / 工程地点
    # 合同名称：优先合同标题（独立短行含“合同”，如“设备采购合同”），非“盖章/材料采购合同供货清单”噪声
    _noise = ("盖章", "供货清单", "争议", "索赔", "变更", "通用条款", "专用条款", "生效", "终止", "保密")
    m = re.search(r"^\s*([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,30}?合同)\s*$", text, re.M)
    if not (m and m.group(1).strip() and not any(k in m.group(1) for k in _noise)):
        m = re.search(r"^\s*([^\n]{2,30}?合同[^\n]{0,20})\s*$", text, re.M)
    if m and m.group(1).strip() and not any(k in m.group(1) for k in _noise):
        result["name"] = m.group(1).strip()
    m = re.search(r"工程名称\s*[:：]?\s*(.+?)(?=\s*工程地点|\n|$)", text)
    if m:
        result["project_name"] = m.group(1).strip()
    m = re.search(r"工程地点\s*[:：]?\s*(.+?)(?=\s*[甲乙]\s*方|\n|$)", text)
    if m:
        result["project_address"] = m.group(1).strip()

    # 合同编号
    m = re.search(r"(?:合同编号|合同号|编号|合同编码)\s*[:：]?\s*([0-9A-Za-z][^\s，,；;、]{1,49})", text)
    if m:
        result["contract_no"] = m.group(1).strip()

    # 甲方 / 乙方（兼容“需方（以下称甲方）：”写法与“甲    方”空格，公司名可含括号）
    def _clean_party(v: str) -> str:
        v = re.sub(r"^(?:采购方|需方|供货方|供方|甲\s*方|乙\s*方)\s*[:：]?\s*", "", v or "").strip()
        return v

    # 甲方 / 乙方（优先“采购方/供货方…（以下称甲方/乙方）”写法）
    m = re.search(r"([^：\n]{2,60}?)\s*[（(]?以下称甲\s*方[）)]", text)
    if m:
        result["party_a"] = _clean_party(m.group(1))
    m = re.search(r"([^：\n]{2,60}?)\s*[（(]?以下称乙\s*方[）)]", text)
    if m:
        result["party_b"] = _clean_party(m.group(1))
    # 兜底“甲方：xxx”写法（兼容“需方（以下称甲方）：”与“甲    方”空格）
    if not result["party_a"]:
        m = re.search(r"甲\s*方\s*[）)]?\s*[:：]?\s*(.+?)(?=\s*(?:乙\s*方|供方)|\n|$)", text)
        if m:
            result["party_a"] = m.group(1).strip()
    if not result["party_b"]:
        m = re.search(r"乙\s*方\s*[）)]?\s*[:：]?\s*(.+?)(?=\s*(?:甲\s*方|需方)|\n|$)", text)
        if m:
            result["party_b"] = m.group(1).strip()

    # 咨询服务/造价咨询等合同：无“甲方/乙方”，用“委托人/咨询人”
    if not result["party_a"]:
        m = re.search(r"委托\s*人\s*[:：]?\s*([^\n，,]{2,60})", text)
        if m:
            result["party_a"] = m.group(1).strip()
    if not result["party_b"]:
        m = re.search(r"咨询\s*人\s*[:：]?\s*([^\n，,]{2,60})", text)
        if m:
            result["party_b"] = m.group(1).strip()

    # 甲方/乙方 税号、开户行、账号（按区域 + 全文兜底）
    def _section(start_marker: str, end_marker: str) -> str:
        s = text.find(start_marker)
        if s == -1:
            return ""
        e = text.find(end_marker, s + 1)
        return text[s: e if e != -1 else len(text)]

    a_sec = _section("甲方", "乙方") or _section("需方", "供方")
    b_sec = _section("乙方", "甲方") or _section("供方", "需方")

    def _extract_tax(sec: str) -> str:
        m = re.search(r"(?:纳税识别号|统一社会信用代码|纳税人识别号|税\s*号)\s*[:：]?\s*([0-9A-Z]{15,18})", sec)
        return m.group(1) if m else ""

    def _extract_bank(sec: str) -> str:
        m = re.search(r"(?:开户行|开户银行|开户\s*行|银行名称)\s*[:：]?\s*([^\s\n，,；;、]{2,40}?(?:银行|支行|分行|营业部))", sec)
        return m.group(1).strip() if m else ""

    def _extract_account(sec: str) -> str:
        m = re.search(r"(?:账\s*号|银行账号|开户账号)\s*[:：]?\s*([0-9][0-9\s]{9,40})", sec)
        if m:
            acct = re.sub(r"\s+", "", m.group(1))
            if 8 <= len(acct) <= 30:
                return acct
        # 兜底：长数字（排除身份证：18 位含生日）
        for m2 in re.finditer(r"(?<![0-9])[0-9]{13,25}(?![0-9])", sec):
            d = m2.group(0)
            if re.fullmatch(r"\d{17}[\dXx]", d) or re.match(r"\d{6}(?:19|20)\d{2}\d{2}\d{3}", d):
                continue
            return d
        return ""

    # 甲方字段：甲方区域优先，兜底全文（附件开票信息常在文末）
    a_tax = _extract_tax(a_sec) or _extract_tax(text)
    a_bank = _extract_bank(a_sec) or _extract_bank(text)
    a_acct = _extract_account(a_sec) or _extract_account(text)
    # 乙方字段：乙方区域优先；全文兜底时避开已归甲方的
    b_tax = _extract_tax(b_sec) or (_extract_tax(text.replace(a_tax, "", 1)) if a_tax else _extract_tax(text))
    b_bank = _extract_bank(b_sec) or (_extract_bank(text.replace(a_bank, "", 1)) if a_bank else _extract_bank(text))
    b_acct = _extract_account(b_sec) or (_extract_account(text.replace(a_acct, "", 1)) if a_acct else _extract_account(text))
    result.update({
        "a_tax_no": a_tax, "b_tax_no": b_tax,
        "a_bank": a_bank, "a_account": a_acct,
        "b_bank": b_bank, "b_account": b_acct,
    })

    # 金额（优先含税金额总计/暂定总价等明确标识，允许整数金额 127200 无小数点）
    # finditer 逐个候选，跳过百分比（“总金额的50%…”不会误当合同总额）
    _amt = _match_amount([
        "含税金额总计", "不含税金额总计", "合同暂定总价", "暂定总价", "暂估总价",
        "合同总价", "合同金额", "总金额", "价款",
    ], text)
    if _amt is None:
        _amt = _match_amount(["合同金额", "合同总价", "合同价款", "总金额", "价款", "金额"], text)
    if _amt is None:
        # 咨询/服务类合同：酬金/人民币后金额
        m = re.search(r"(?:酬金|人民币|费用)\s*[:：]?\s*[￥¥]?\s*(\d[\d,]*)(?:\.\d{1,2})?\s*元", text)
        if m:
            try:
                _amt = float(m.group(1).replace(",", ""))
            except (ValueError, TypeError):
                _amt = None
    if _amt is not None:
        result["amount"] = _amt

    # 签订日期（排除“开工日期/竣工日期/计划…”）
    m = re.search(r"(?<!开工)(?<!竣工)(?<!完工)(?<!计划)(?:签订日期|签约日期|合同签订日期|签订时间|签订于|日期)\s*[:：]?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        try:
            result["sign_date"] = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 工期：开工 / 竣工日期
    def _date_like(s: str) -> date | None:
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        return None

    m = re.search(r"(?:开工日期|开工时间|计划开工|进场日期|开工)\s*[:：]?\s*[^\n。；;]{0,40}?(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})", text)
    result["start_date"] = _date_like(m.group(1)) if m else None
    m = re.search(r"(?:竣工日期|完工日期|计划完工|竣工时间|完工)\s*[:：]?\s*[^\n。；;]{0,40}?(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})", text)
    result["end_date"] = _date_like(m.group(1)) if m else None

    # 工期天数（"工期 X 天 / 日历天"）；若无则按 开工-竣工 计算
    m = re.search(r"工期\s*[:：]?\s*(\d{1,4})\s*(?:个\s*)?(?:日历)?天", text)
    if m:
        result["duration_days"] = int(m.group(1))
    elif result["start_date"] and result["end_date"]:
        result["duration_days"] = (result["end_date"] - result["start_date"]).days

    # 付款方式（“付款方式：银行转账”“付款方式为转账汇款”等；若无但存在开户银行则推断为银行转账）
    m = re.search(r"付款方式\s*[:：]?\s*[为是]?\s*([^0-9\s\n，,。；;]{2,20})", text)
    if m:
        result["pay_method"] = m.group(1).strip()
    if not result["pay_method"] and re.search(r"开户银行|开户行", text):
        result["pay_method"] = "银行转账"

    # 付款节点表（表格行：款项|比例|金额|时间|备注，如“1、预付款\t20\t411.71\t2024.03.01\t…”）
    def _fnum(v):
        try:
            return float(str(v).replace(",", "").replace("万元", "").strip()) if str(v).strip() not in ("", "/", "—") else 0
        except (ValueError, TypeError):
            return 0

    pay_terms = []
    for line in text.split("\n"):
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) >= 2 and re.match(r"^\d+、?\s*[^\t]*?(预付款|进度款|竣工款|结算款|质保款|工程款|验收款)", cells[0]):
            name = re.sub(r"^\d+、?\s*", "", cells[0])
            rate = _fnum(cells[1]) if len(cells) > 1 else 0
            amt = _fnum(cells[2]) if len(cells) > 2 else 0
            pay_date = cells[3] if len(cells) > 3 else ""
            cond = cells[4] if len(cells) > 4 else ""
            pay_terms.append({"name": name, "rate": rate, "amount_wan": amt,
                              "pay_date": pay_date, "condition": cond})
    result["pay_terms"] = pay_terms
    if pay_terms and pay_terms[0]["rate"]:
        result["prepay_rate"] = pay_terms[0]["rate"]

    # 段落式定金/预付款（如“支付合同总价的 20% 定金…340000.00 元”）
    if not pay_terms:
        m = re.search(r"(?:支付合同总价的|合同总价的|总价的)\s*(\d+(?:\.\d+)?)\s*%\s*(?:定金|预付款)", text)
        if m:
            result["prepay_rate"] = float(m.group(1))
            _dep = {"name": "定金", "rate": float(m.group(1)),
                    "amount_wan": round(result["amount"] * float(m.group(1)) / 100 / 10000, 2),
                    "pay_date": "", "condition": "签署合同后按约定支付"}
            result["pay_terms"].append(_dep)
        elif not result["prepay_rate"]:
            m2 = re.search(r"(?:定金|预付款).*?([\d.]+)%?\s*[，,]?\s*[^\n]{0,10}?([\d,.]+)\s*元", text)
            if m2:
                result["prepay_rate"] = float(m2.group(1))

    # 税率（如 13% 增值税）
    m = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%\s*增值税|增值税.*?(\d{1,2})\s*%", text)
    if m:
        result["tax_rate"] = float(m.group(1) or m.group(2))

    # 预付款比例（兼容“按合同暂定总金额的 50 %支付预付款”等写法）
    m = re.search(r"(?:预付款|合同暂定总金额|总金额)[^\n]{0,30}?(\d{1,3})\s*%", text)
    if m:
        result["prepay_rate"] = float(m.group(1))

    # 质保期（月）：兼容“保修期 X 年”
    m = re.search(r"(?:保用期|质保期|保修期)[^\n]{0,30}?(\d{1,3})\s*(?:个)?月", text)
    if m:
        result["warranty_months"] = int(m.group(1))
    else:
        m = re.search(r"(?:保用期|质保期|保修期)[^\n]{0,30}?(\d{1,3})\s*年", text)
        if m:
            result["warranty_months"] = int(m.group(1)) * 12

    # 质保金比例（“质保款 3% / 结算金额*3% / 质保金比例：…%）
    m = re.search(r"质保[款金][^\n]{0,40}?(\d{1,3}(?:\.\d+)?)\s*%|结算金额\s*\*\s*(\d{1,3}(?:\.\d+)?)\s*%", text)
    if m:
        result["warranty_retention_rate"] = float(m.group(1) or m.group(2) or 0)
    m = re.search(r"质保金比例\s*[:：]?\s*结算总价的\s*(\d{1,3}(?:\.\d+)?)\s*%", text)
    if m:
        result["warranty_retention_rate"] = float(m.group(1))

    # 甲乙双方联系人/电话（甲方指定 XX 为合同履行的代表，联系电话：xxx）
    m = re.search(r"甲方指定\s*([^\s，,。；;]{2,20})\s*为合同履行的代表[^\n]{0,60}?联系电话\s*[:：]?\s*([\d\s-]{6,20})", text)
    if m:
        result["a_contact"] = f"{m.group(1)} {m.group(2).strip()}"
    m = re.search(r"乙方指定\s*([^\s，,。；;]{2,20})\s*为合同履行的代表[^\n]{0,60}?联系电话\s*[:：]?\s*([\d\s-]{6,20})", text)
    if m:
        result["b_contact"] = f"{m.group(1)} {m.group(2).strip()}"

    def _to_float(v):
        try:
            if v is None or str(v).strip() == "":
                return 0.0
            return float(str(v).replace(",", "").replace("￥", "").replace("¥", "").strip())
        except (ValueError, TypeError):
            return 0.0


    def _parse_materials(text: str) -> list[dict]:
        """解析合同采购清单（表格行：序号|货物名称|规格|单位|数量|单价|金额）。"""
        materials = []
        lines = text.split("\n")
        for i, line in enumerate(lines):
            cells = [c.strip() for c in line.split("\t")]
            if len(cells) < 3:
                continue
            if not (any(any(k in c for k in ["货物名称", "材料名称", "品名", "名称"]) for c in cells)
                    and any("数量" in c for c in cells)):
                continue
            name_i = next((j for j, c in enumerate(cells) if any(k in c for k in ["货物名称", "材料名称", "品名", "名称"])), -1)
            spec_i = next((j for j, c in enumerate(cells) if any(k in c for k in ["规格", "型号"])), -1)
            brand_i = next((j for j, c in enumerate(cells) if "品牌" in c), -1)
            unit_i = next((j for j, c in enumerate(cells) if "单位" in c), -1)
            qty_i = next((j for j, c in enumerate(cells) if "数量" in c), -1)
            price_i = next((j for j, c in enumerate(cells) if "单价" in c), -1)
            amount_i = next((j for j, c in enumerate(cells) if "小计" in c or "金额" in c), -1)
            if name_i < 0 or qty_i < 0:
                continue
            for j in range(i + 1, len(lines)):
                row = [c.strip() for c in lines[j].split("\t")]
                if not any(row):
                    continue
                if row and any(k in row[0] for k in ["合计", "总计", "小计"]):
                    break
                name = row[name_i] if name_i < len(row) else ""
                if not name or name in {"合计", "总计", "小计"}:
                    continue
                qty = _to_float(row[qty_i]) if qty_i < len(row) else 0.0
                if qty <= 0:
                    continue
                materials.append({
                    "name": name,
                    "spec": row[spec_i] if 0 <= spec_i < len(row) else "",
                    "brand": row[brand_i] if 0 <= brand_i < len(row) else "",
                    "unit": (row[unit_i] if 0 <= unit_i < len(row) else "") or "件",
                    "quantity": qty,
                    "unit_price": _to_float(row[price_i]) if 0 <= price_i < len(row) else 0.0,
                    "amount": _to_float(row[amount_i]) if 0 <= amount_i < len(row) else 0.0,
                })
            break
        return materials


# 时间节点：遍历文本中的绝对日期，取日期前最近出现的节点关键词作为节点名
    # （同日期只生成一个节点，避免“首付款/首付/付款/支付”重复）
    seen_dates = set()
    for m in re.finditer(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if d in seen_dates:
            continue
        seen_dates.add(d)
        before = text[max(0, m.start() - 60):m.start()]
        kw, last_pos = None, -1
        for k in MILESTONE_KEYWORDS:
            p = before.rfind(k)
            if p > last_pos:
                last_pos = p
                kw = k
        if kw:
            result["milestones"].append({"name": kw, "m_date": d, "amount": 0.0})
    result["milestones"].sort(key=lambda x: x["m_date"])
    result["materials"] = _parse_materials(text)

    # 若未识别出合同名称，用合同编号/甲方乙方组合
    if not result["name"]:
        result["name"] = result["contract_no"] or f"{result['party_b'] or '采购'}合同"

    return result


def milestone_kind(name: str) -> str:
    """节点类型分类，用于甘特图颜色。"""
    if name == "签约":
        return "签约"
    if any(k in name for k in ("付款", "支付", "首付", "尾款", "进度款")):
        return "付款"
    if any(k in name for k in ("交货", "到货", "供货", "交付", "进场")):
        return "交货"
    if any(k in name for k in ("验收", "完工", "竣工", "安装")):
        return "验收"
    if "质保" in name:
        return "质保"
    return "其他"


KIND_COLORS = {
    "签约": "#8FA9B8",   # 灰蓝
    "付款": "#A3B18A",   # 豆沙绿
    "交货": "#C9915F",   # 脏橘
    "验收": "#C3A6A6",   # 藕粉
    "质保": "#CBB895",   # 燕麦黄
    "其他": "#A79BAF",   # 雾霾灰紫
}
