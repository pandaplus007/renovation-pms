"""金额转中文大写。

为什么必须有
------------
纸质签证单上只有小写金额（¥12,345.00）不认——手写涂改或事后补写时
「¥12,345」和「¥13,245」只差一个数字，扯皮时无从对证。中文大写
（壹万贰仟叁佰肆拾伍元整）是防涂改的约定写法，签字单上必须印。

三个坑（都真踩过）
------------------
1. **段内数字顺序**。按「个位→高位」迭代时，个位没有单位（UNITS[0]=""），
   若也用「前插」就会把个位数字塞到最前面：11 会变成「壹壹拾」。
   正确做法是每段的四位从高位往低位**拼成序列再拼接**，个位留在末尾。
2. **组间的零**。10001 是「壹万零壹」而不是「壹万壹」；100001000
   中间整段为 0 也要补零：「壹亿零壹仟」。漏补的「零」会让 1 万和
   1 万 1 千读起来一样。
3. **浮点取整**。1.005 * 100 在 IEEE754 下是 100.49999999999999，
   round() 得 100 分，1.005 元被吞成 1.00 元。金额必须用 Decimal 的
   ROUND_HALF_UP——财务口径是「四舍五入」，不是 Python 的银行家舍入。

写法约定
--------
- 零要写出来，不能留空：0 → 零元整
- 「元」与「角」之间即使无小数也要补「整」：12 → 壹拾贰元整
- 角为 0 而分不为 0 时用「零」衔接：12.05 → 壹拾贰元零伍分
  （漏掉这个「零」就是 12 元 5 分与 125 元两种读法）
- 负数加「负」前缀：减项在变更签证里是常见情形
- 十/百/千的首位保留「壹」（壹拾而非拾）：财务规范如此，
  且「拾万」易被读成十万以下，避免跨位误读
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

DIGITS = "零壹贰叁肆伍陆柒捌玖"
UNITS = ("仟", "佰", "拾", "")
GROUPS = ("", "万", "亿", "万亿")


def _chunk_to_cn(chunk: int) -> str:
    """四位以内的一段转中文（不含组单位）。1005 → 壹仟零伍。"""
    ds = (chunk // 1000 % 10, chunk // 100 % 10, chunk // 10 % 10, chunk % 10)
    out: list[str] = []
    zero = False
    for i, d in enumerate(ds):
        if d == 0:
            # 只在**已有内容之后**的 0 才需要考虑补零；开头的 0 直接跳过
            if out:
                zero = True
            continue
        if zero:
            # 中间出现过 0：补且只补一个「零」（连续多个 0 合并成一个）
            out.append("零")
            zero = False
        out.append(DIGITS[d] + UNITS[i])
    return "".join(out)


def _int_to_cn(n: int) -> str:
    """非负整数转中文（不含「元整」）。0 返回「零」。"""
    if n == 0:
        return "零"

    segs = []
    while n > 0:
        segs.append(n % 10000)
        n //= 10000

    out: list[str] = []
    zero_pending = False
    for gi in range(len(segs) - 1, -1, -1):
        chunk = segs[gi]
        if chunk == 0:
            # 整段为零：记下来，下一个非零段要补「零」
            zero_pending = True
            continue
        txt = _chunk_to_cn(chunk) + GROUPS[gi]
        if out:
            # 补零的两个条件，满足其一即可：
            #   · 上一段与本段之间夹着全零段
            #   · 本段不足四位（高位有 0），如 1亿 + 2万 要写「壹亿零贰万」
            if (zero_pending or chunk < 1000) and not out[-1].endswith("零"):
                txt = "零" + txt
        out.append(txt)
        zero_pending = False
    return "".join(out)


def amount_to_cn(value) -> str:
    """金额转中文大写，如 12345.5 -> 壹万贰仟叁佰肆拾伍元伍角。

    负数（减项）加「负」前缀。None / 空值 / 非法值按 0 处理，写成「零元整」
    ——签字单的金额栏不能是空的，空着等于没填。
    """
    try:
        d = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, ValueError, TypeError):
        d = Decimal(0)

    neg = d < 0
    d = abs(d)
    # 量化到分，用 ROUND_HALF_UP（财务口径）。不能先转 float——那一步
    # 就会把 1.005 变成 1.00499999…，后面再怎么舍都回不来。
    cents = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    yuan, rest = divmod(cents, 100)
    jiao, fen = divmod(rest, 10)

    out = _int_to_cn(yuan) + "元"
    if rest == 0:
        out += "整"
    else:
        if jiao:
            out += DIGITS[jiao] + "角"
        elif fen:
            # 角为 0、分不为 0：必须补「零」，否则 12.05 会被读成 12 元 5 分
            out += "零"
        if fen:
            out += DIGITS[fen] + "分"
    return ("负" if neg else "") + out