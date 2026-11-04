"""财务严谨性：口径措辞一致性。

这轮修的是三个**事实错误**，不是风格问题：

1. 对账页同一个状态有四种写法（流水有、系统没 / 流水有、系统没有 /
   流水有·系统无 / …）。同一件事在同一页换个说法，读者会以为是两件事。
2. 两种「对不上」的严重程度不同却用同一档色：
   「流水有、系统没有」= 钱付了但系统没记 → **账虚高**，要动手补录；
   「系统有、流水没有」= 多数只是漏传凭证或银行未扣款 → 不一定有问题。
   原来一个琥珀一个灰，读者会以为后者不重要。
3. 毛利报表的列名不平行：前几列都标口径（已下单/已签约），
   后两列却用技术词「下限」——看表的人不会自动知道它是更保守的那个数。

这些都属于「写错了」，所以断言要卡死具体措辞，而不是「看起来统一」。
"""
from __future__ import annotations

import glob
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "app", "templates")

P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


BANK = os.path.join(TPL, "reconcile", "bank.html")
PROFIT = os.path.join(TPL, "reports", "profit.html")

bank = io.open(BANK, encoding="utf-8", errors="replace").read()
profit = io.open(PROFIT, encoding="utf-8", errors="replace").read()


def strip_jinja_comments(s: str) -> str:
    """去掉 {# ... #} 注释。

    必须去：断言「页面正文里不出现某个词」时，注释里为了解释「为什么改成
    这样」而提到那个词，会让断言假失败。上一版就栽在这里——
    「下限」已经从表头和标签里删干净了，只剩注释里解释它为何被替换，
    断言却报它还在。
    """
    return re.sub(r"\{#.*?#\}", "", s, flags=re.S)


profit_body = strip_jinja_comments(profit)
bank_body = strip_jinja_comments(bank)

print("=" * 78)
print("[1] 对账页：同一个状态只有一种写法")
print("=" * 78)
# 关键修正：上一版用 r"流水有[^<\"'\n]{0,8}" 会把措辞后面紧跟的标点
# （「流水有、系统没有，」中的逗号）也算进措辞本身，于是同一个词被判成
# 多个变体。这里只取「流水有/系统有 + 直到顿号或逗号为止」的部分。
WORD = r"(?:流水有|系统有)[^、，。（）]*"


def variants_of(text: str, prefix: str) -> dict:
    """收集以 prefix 开头、到标点为止的措辞变体。

    注意截断字符集里**不能含「、」**：两个状态的措辞本身都带顿号
    （「流水有、系统没有」），一旦在顿号处截断，就只能截到「流水有」，
    后面真正要区分的「、系统没有」全丢了——于是这个函数会得出
    「只有一个变体」的假象。
    """
    out = {}
    for m in re.finditer(prefix + r"[^，。；（）\n<\"']*", text):
        w = m.group(0).strip()
        if w:
            out[w] = out.get(w, 0) + 1
    return out


v1 = variants_of(bank_body, "流水有")
v2 = variants_of(bank_body, "系统有")
print("  「流水有」相关：%s" % v1)
print("  「系统有」相关：%s" % v2)
# 「系统有」还会匹配到「流水有、系统没有」后半段的那个「系统没有」，
# 那是同一状态的描述用词，不是第二个状态。只取以「系统有、」开头的。
v2_state = {w for w in v2 if w.startswith("系统有、")}
check("「流水有…」只有一种写法（%s）" % sorted(v1), len(v1) == 1, sorted(v1))
check("「系统有、流水没有」只有一种写法（%s）" % sorted(v2_state),
      len(v2_state) == 1, sorted(v2_state))
check("两个状态都在页面上出现",
      bool(v1) and bool(v2_state), (v1, v2_state))
check("两个状态的措辞不会互相嵌套",
      not re.search(r"流水有、系统有(?!有)", bank_body),
      re.findall(r"流水有、系统有(?!有)", bank_body))

print()
print("=" * 78)
print("[2] 两种不对称的严重程度要写清后果")
print("=" * 78)
check("「流水有、系统没有」说明账会虚高",
      "虚高" in bank, [x for x in bank.split("\n") if "虚高" in x][:2])
check("「系统有、流水没有」说明不一定有问题",
      "不一定有问题" in bank or "未必" in bank,
      [x for x in bank.split("\n") if "不一定" in x][:2])
check("两者用「需补录」/「需核对」区分",
      "需补录" in bank and "需核对" in bank)
# 严重的那档必须比不严重的那档视觉更强：amber vs gray
check("需补录用警示色（--warning-ink）",
      "--warning-ink" in bank)
check("需核对用中性灰（不抢眼）",
      "需核对" in bank and 'class="fs-4 text-muted"' in bank)

print()
print("=" * 78)
print("[3] 毛利报表：两个口径用同一种写法")
print("=" * 78)
check("正文里不再出现「下限」这个技术词（注释里的解释不算）",
      "下限" not in profit_body,
      [x for x in profit_body.split("\n") if "下限" in x][:3])
# 表头里两处毛利、两处毛利率，且都带口径标注。
# 注意不能用 "毛利" in h 来筛：它会把「毛利率」也算进来，于是数成 4 列。
# ��用精确匹配——把 th 内的标签去掉后先剥掉「率」字再比对。
heads = re.findall(r"<th[^>]*>(.*?)</th>", profit_body, re.S)
gross = []
for h in heads:
    txt = re.sub(r"<[^>]+>", " ", h)
    txt = re.sub(r"\s+", " ", txt).strip()
    if txt.startswith("毛利") and not txt.startswith("毛利率"):
        gross.append(txt)
print("  「毛利」列：%s" % gross)
check("毛利恰好 2 列（已下单/已签约两个口径）", len(gross) == 2, gross)
check("两个毛利列都标了口径",
      all(("已下单" in g or "已签约" in g) for g in gross), gross)

rate = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()
        for h in heads
        if re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()
        .startswith("毛利率")]
print("  「毛利率」列：%s" % rate)
check("毛利率恰好 2 列", len(rate) == 2, rate)

print()
print("=" * 78)
print("[4] 每个数字自己解释自己")
print("=" * 78)
# 两栏对照卡各自要有一句「这个数是怎么来的」
for label, must in [("乐观", "还没花出去"), ("保守", "全部按已签约价格花掉")]:
    check("「%s」口径旁解释了算法" % label, must in profit,
          [x for x in profit.split("\n") if must in x][:1])
check("标出了乐观/保守", "乐观" in profit and "保守" in profit)

print()
print("=" * 78)
print("[5] 次要列：表头与单元格必须一一对应")
print("=" * 78)
# col-minor 数量不一致会导致收起后列错位。
# 上一版正则是 r"<thead>(.*?)</thead>"，而本模板的 <thead> 与 <tr> 之间有换行，
# 且表格标签带换行属性（data-col-minor 那几行），直接匹配会 None。
# 改成先剥注释，再用 re.S 匹配整个 thead 块。
# 本模板的 <thead> 带 class 属性（<thead class="table-light">），
# 所以正则要允许属性，不能写死 "<thead>"。上一版就是这里没匹配上，
# 导致 head_minor=0，进而把 tbody 的正常单元格全判成「次要列数不符」。
m = re.search(r"<thead(?:\s[^>]*)?>(.*?)</thead>", profit_body, re.S)
check("找到表头块", m is not None)
head_minor = len(re.findall(r'<th[^>]*class="[^"]*col-minor', m.group(1))) if m else 0
print("  表头次要列：%d" % head_minor)
check("表头标了次要列", head_minor > 0, head_minor)

# tbody 与 tfoot 各行的次要列数必须等于表头
for tag in ("tbody", "tfoot"):
    mm = re.search(r"<%s>(.*?)</%s>" % (tag, tag), profit_body, re.S)
    if not mm:
        continue
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", mm.group(1), re.S)
    for k, row in enumerate(rows, 1):
        tds = re.findall(r"<td[^>]*>", row)
        minor = sum(1 for t in tds if "col-minor" in t)
        if len(tds) < 5:
            continue
        check("%s 第 %d 行：%d 个单元格，%d 个次要列（应等于表头 %d）"
              % (tag, k, len(tds), minor, head_minor),
              minor == head_minor)

check("表格启用了次要列开关",
      "data-col-minor" in profit and "data-col-key" in profit)

print()
print("=" * 78)
print("[6] 类别标签仍用中性色（不该被状态色污染）")
print("=" * 78)
# 这几处是上一轮特意留下的：类别标签与计数芯片不是业务状态
CATEGORY_OK = {
    "oa/permissions.html": "自定义",
    "projects/detail.html": "doc_type",
    "quotes/index.html": "counts",
    "sales_invoices/index.html": "counts",
}
leaked = []
for rel, marker in CATEGORY_OK.items():
    s = io.open(os.path.join(TPL, rel.replace("/", os.sep)), encoding="utf-8",
                errors="replace").read()
    for m in re.finditer(r'badge[^"]*"[^>]*>\s*\{\{\s*' + re.escape(marker), s):
        cls = s[max(0, m.start() - 120):m.start()]
        if "state-" in cls.split("badge")[-1]:
            leaked.append((rel, marker))
check("类别标签未被状态色污染", not leaked, leaked)

print()
print("=" * 78)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
print("=" * 78)
sys.exit(1 if F else 0)