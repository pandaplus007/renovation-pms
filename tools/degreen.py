"""把残余的绿调色值按「保留明度、色相转到蓝族」批量换掉。

为什么上一轮漏了 36 处
----------------------
第一版脚本用 COLOR_MAP 逐个列举要替换的色值，漏掉的是那些**饱和度很低**
的浅绿（如 #eef5f1 的 g−b 只差 4）。我当时用「色相在 95~175 且 g−b > 12」
当兜底判据，于是它们从缝里漏了过去——而 #eef5f1 恰好是**表格表头底色**，
是全站出现次数最多的浅绿。

教训：枚举法必然漏。改成**按规则扫**（色相落在绿色区间就换），
再把成功语义色显式排除。

换色规则
--------
用 colorsys 把 HSL 的 **L 原样保留**，色相转到主色的 214°，
饱和度小幅补偿（绿转蓝后视觉饱和度会偏低）。
保留明度是刻意的：换色不能改变元素的层级关系，
否则「主色深、辅助色浅」的次序会被打乱，界面反而更难读。

成功语义色（绿=已完成/正常/收款）**保留**——它们表达的是业务含义，
不是品牌。把「已完成」调成蓝色会和「进行中」撞色，状态色就废了。
"""
from __future__ import annotations

import colorsys
import io
import re
import sys

PATH = "app/static/css/style.css"
TARGET_HUE = 214.0

# 绿色语义色：必须保留
KEEP = {
    "#10b981",  # --success（徽章底）
    "#1d6b52",  # --success-ink
    "#34d399",  # 深色主题 --success-ink
    "#e7f4ef",  # --success-bg
    "#14332a",  # 深色主题 --success-bg
}


def hue_of(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return None
    d = mx - mn
    if mx == r:
        hp = ((g - b) / d) % 6
    elif mx == g:
        hp = (b - r) / d + 2
    else:
        hp = (r - g) / d + 4
    return hp * 60


def to_blue(hexs: str) -> str:
    h = hexs.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    _, l, s = colorsys.rgb_to_hls(r, g, b)
    # 极低饱和的（接近中性灰）保持低饱和，否则会变成明显的蓝块
    ns = min(1.0, s * 1.06) if s > 0.02 else s
    nr, ng, nb = colorsys.hls_to_rgb(TARGET_HUE / 360.0, l, ns)
    return "#%02x%02x%02x" % (round(nr * 255), round(ng * 255), round(nb * 255))


src = io.open(PATH, encoding="utf-8").read()

# 标出每个字符是否位于注释内，只替换**活代码**里的色值。
#
# 不用「按行判断」：一行完全可能是
#     background: #eef5f1;  /* 浅绿表头 */
# 这种前面是活代码、后面带行尾注释的写法。按行处理要么整行跳过（漏掉
# #eef5f1，它是全站表头底色，出现次数最多），要么误改注释里的举例。
# 必须做到字符级。
in_comment = [False] * len(src)
i, n, c = 0, len(src), False
while i < n:
    if not c and src.startswith("/*", i):
        c = True
        in_comment[i] = True
        in_comment[i + 1] = True
        i += 2
        continue
    if c and src.startswith("*/", i):
        in_comment[i] = True
        in_comment[i + 1] = True
        c = False
        i += 2
        continue
    in_comment[i] = c
    i += 1

changes = []


def repl(m, base):
    if in_comment[m.start()]:
        return m.group(0)
    h = "#" + m.group(1).lower()
    if h in KEEP:
        return m.group(0)
    r = int(m.group(1)[0:2], 16)
    g = int(m.group(1)[2:4], 16)
    b = int(m.group(1)[4:6], 16)
    hu = hue_of(r, g, b)
    if hu is None or not (95 <= hu <= 175):
        return m.group(0)
    new = to_blue(h)
    ln = src.count("\n", 0, base + m.start()) + 1
    changes.append((ln, h, new, src.split("\n")[ln - 1].strip()[:54]))
    return new


out = re.sub(r"#([0-9a-fA-F]{6})\b", lambda m: repl(m, 0), src)

print("=" * 76)
print("绿调 → 蓝调（%d 处）" % len(changes))
print("=" * 76)
for i, old, new, ctx in changes:
    print("  %5d  %s -> %s   %s" % (i, old, new, ctx))

# ---- 自检 ----
print()
print("=" * 76)
print("自检")
print("=" * 76)
code_only = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                   out, flags=re.S)
left = []
for m in re.finditer(r"#([0-9a-fA-F]{6})\b", code_only):
    h = "#" + m.group(1).lower()
    if h in KEEP:
        continue
    r, g, b = (int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    hu = hue_of(r, g, b)
    if hu is not None and 95 <= hu <= 175:
        left.append(h)
print("  残留绿调色值：%d 处 %s" % (len(left), left[:8]))
print("  成功语义色保留：%s"
      % ("是" if all(k in code_only for k in ("#10b981", "#1d6b52", "#e7f4ef")) else "!! 丢失"))

io.open(PATH, "w", encoding="utf-8", newline="").write(out)
print()
print("已写入 %s" % PATH)