"""审计 style.css 里所有硬编码色值。

⚠ 这是**可维护性报告**，不是正确性闸门。
  判定「某个颜色该不该出现在规则里」靠启发式，会有大量「设计上就该
  硬编码」的情况（莫兰迪强调色、统计卡的装饰色卡、徽章底色）。
  真正判定「看不看得清 / 有没有错」的是这两个**测试**：
      tests/test_contrast_wcag.py        —— WCAG 对比度，两个主题
      tests/test_no_light_surfaces_in_dark.py —— 深色下不许有浅色面
  这个脚本的价值是**发现换色时的漏网**（见下）。

它为什么值得存在：本轮它查出了 **25 处 rgb(62,108,168)**，
也就是被否决的那个蓝 #3E6CA8。上一轮换色只查了「绿色残留 = 0」，
没查蓝色残留，所以「已彻底换完」那个结论是假的。
这些蓝的透明度都在 .05~.35，肉眼几乎看不出——
低透明度的错色色恰恰是最容易长期存活的一类。

分类维度
--------
  1. 出现在 :root / 深色块 / print 回退块里 → 合法（它们是**定义处**）
  2. 落在语义色里 → 合法（success / warning / danger / info）
  3. 落在中性黑白灰里 → 合法
  4. 落在近黑里 → 合法（深色主题的阴影/描边）
  5. 落在**被否决的蓝**里 → !! 严重，必须换
  6. 其余 → 「需人工判断」：多数是莫兰迪系的设计色，不是错误
"""
from __future__ import annotations

import io
import re
from collections import defaultdict

PATH = "app/static/css/style.css"
s = io.open(PATH, encoding="utf-8").read()

# ---- 剥掉注释，避免注释里的色值干扰统计 ----
# CSS 注释**不嵌套**，遇 */ 即止，所以这里用朴素的 find/前移。
def strip_comments(t):
    out, i, n = [], 0, len(t)
    while i < n:
        if t.startswith("/*", i):
            j = t.find("*/", i + 2)
            if j < 0:
                break
            out.append("\n" * t.count("\n", i, j))
            i = j + 2
        else:
            out.append(t[i])
            i += 1
    return "".join(out)


code = strip_comments(s)

# ---- 定义区：:root、深色变量块、print 回退块。它们的颜色都算「合法」 ----
# 只把 :root 算作合法是不够的：深色主题有一整套自己的色阶，
# 不认它的话深色块里的每个颜色都会被报成可疑，输出被噪音淹没。
def block_span(selector, nth=0):
    hits = list(re.finditer(re.escape(selector) + r"\s*\{", code))
    if len(hits) <= nth:
        return None
    st = hits[nth].end() - 1
    d, i = 0, st
    while i < len(code):
        if code[i] == "{":
            d += 1
        elif code[i] == "}":
            d -= 1
            if d == 0:
                break
        i += 1
    return (st, i + 1, code[st : i + 1])


defs = []
r = block_span(":root", 0)
if r:
    defs.append(r)
for nth in range(6):
    b = block_span('[data-bs-theme="dark"]', nth)
    if b:
        defs.append(b)

LEGAL = set()
for a, bnd, body in defs:
    for m in re.finditer(r"#([0-9a-fA-F]{3,6})\b", body):
        h = m.group(1).lower()
        # ⚠ 补上前面的 '#'：正则捕获的是**不含 #** 的色值，
        #   而下面查表用的键是 '#rrggbb'。少了这一行，
        #   71 组全是误报——连品牌色 #3d7a63 都被标成「可疑」，
        #   整个工具的输出就没有参考价值了。
        LEGAL.add("#" + (h if len(h) == 6 else "".join(c * 2 for c in h)))
for extra in ("#ffffff", "#000000", "#000", "#fff"):
    LEGAL.add(extra)

# 语义色族（:root 里 --success/--warning/--danger 的字面值 + 其 rgb 形式）
SEMANTIC_RGB = [(16, 185, 129), (245, 158, 11), (239, 68, 68)]  # #10b981 #f59e0b #ef4444
DANGER_RGB = [(248, 113, 113), (252, 165, 165)]                   # 深色 danger
SUCCESS_RGB = [(52, 211, 153), (110, 231, 183)]
WARN_RGB = [(251, 191, 36), (252, 211, 77)]

print("合法色（三个定义区合计 %d 支）" % len(LEGAL))


def in_def(pos):
    """⚠ 第一版写的是 `0 <= pos < len(root_body)`——拿**绝对偏移**
       跟 root_body 的**长度**比，等于只检查了文件开头一小段，
       于是 :root 自己的 30 处品牌绿全被报成「外部硬编码」。
       正确写法是拿区间 [start, end) 判断。"""
    return any(a <= pos <= bnd for a, bnd, _ in defs)


def hx2rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


REJECTED_BLUE = (62, 108, 168)      # #3E6CA8 被否决的蓝
found = defaultdict(list)           # rgb -> [(行号, 片段)]

for m in re.finditer(r"#([0-9a-fA-F]{6})\b", code):
    if in_def(m.start()):
        continue
    rgb = hx2rgb(m.group(0))
    ln = code[:m.start()].count("\n") + 1
    found[rgb].append((ln, "#" + m.group(1)))

for m in re.finditer(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", code):
    if in_def(m.start()):
        continue
    rgb = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    ln = code[:m.start()].count("\n") + 1
    found[rgb].append((ln, "rgb(%d,%d,%d)" % rgb))

print()
print("=" * 74)
print("定义区之外的硬编码色值，按色相归类")
print("=" * 74)

suspicious = []
for rgb, locs in sorted(found.items(), key=lambda kv: -len(kv[1])):
    hx = "#%02x%02x%02x" % rgb
    if hx.lower() in LEGAL:
        tag = "OK  :root 色阶里"
    elif rgb == REJECTED_BLUE:
        tag = "!!  被否决的蓝 #3E6CA8 残留"
        suspicious.append((rgb, locs, tag))
    elif rgb in SEMANTIC_RGB or rgb in DANGER_RGB or rgb in SUCCESS_RGB or rgb in WARN_RGB:
        tag = "OK  语义色"
    elif abs(rgb[0] - rgb[1]) < 6 and abs(rgb[1] - rgb[2]) < 6:
        tag = "OK  中性灰"
    elif rgb[0] < 60 and rgb[1] < 60 and rgb[2] < 60:
        tag = "OK  近黑（深色主题的阴影/描边）"
    else:
        tag = "??  可疑，需人工判断"
        suspicious.append((rgb, locs, tag))
    print("  %-9s %-3d 处  %s" % (hx, len(locs), tag))
    for ln, txt in locs[:6]:
        line = s.split("\n")[ln - 1].strip()
        print("        行 %5d  %s" % (ln, line[:78]))

print()
print("=" * 74)
print("需要人工判断的：%d 组" % len(suspicious))
print("=" * 74)
for rgb, locs, tag in suspicious:
    print("  %s  %s  （%d 处）" % ("#%02x%02x%02x" % rgb, tag, len(locs)))
    for ln, _ in locs:
        print("      行 %5d  %s" % (ln, s.split("\n")[ln - 1].strip()[:78]))