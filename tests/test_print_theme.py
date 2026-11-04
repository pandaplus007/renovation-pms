"""打印回退检查：深色主题独有的变量，**每一个**都必须在 @media print 里复位。

为什么必须自动化
----------------
`@media print` 块里有一段把深色变量改回浅色的兜底。它存在的原因是：
深色主题下直接 Ctrl+P 会印出一整页深绿/深黑底——浪费碳粉，而且表格线与
淡色文字全印不出来（打印机按 CMYK 还原深底更糟）。

但这段兜底是**手工维护的**：每往深色块里加一个变量，就得记得在这里补一行，
忘了没有任何提示。而漏掉的表现非常隐蔽：
  · 屏幕上看不出来（深色主题本来就是那些值）
  · 服务端测试全绿
  · 只有**真去打印**才看到——印出来的进度条/按钮是一块脏灰色块，
    因为金色按 CMYK 还原会偏色，而 print 块里的
    `a{color:#000!important}` 管不到 progress-bar / btn。

所以这里做静态检查：找出「只在深色块定义、没在 print 块复位」的变量。

写法上有个坑：CSS 注释**不嵌套**，遇到第一个 `*/` 就结束。
本文件用朴素的 find/配平，不用正则去切注释——上一轮正是因为在注释里
写了注释定界符，整段规则被静默吞掉。
"""
from __future__ import annotations

import io
import re
import sys

PATH = "app/static/css/style.css"
s = io.open(PATH, encoding="utf-8").read()


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


def block_body(selector, nth=0):
    """取第 nth 个 selector { ... } 的块体（按花括号配平，不用正则切）。"""
    hits = list(re.finditer(re.escape(selector) + r"\s*\{", t))
    if len(hits) <= nth:
        return None
    st = hits[nth].end() - 1
    d, i = 0, st
    while i < len(t):
        if t[i] == "{":
            d += 1
        elif t[i] == "}":
            d -= 1
            if d == 0:
                break
        i += 1
    return t[st : i + 1]


t = strip_comments(s)

DARK = '[data-bs-theme="dark"]'

# 每个块**所在行**的花括号深度。
# 深度 0 = 顶层；深度 ≥1 = 在某个 @media 里（print 兜底块就在这）。
# 为什么必须按深度判而不是「找第一个 dark 块」：
# 一开始我就是靠「向上找最近的 @media 行」来判断归属，
# 结果把行 574 的顶层深色变量块误判成在 @media print 里——
# 因为注释里也出现过 @media 字样。只有花括号配平才作数。
lines = t.split("\n")
depth_at = {}
d = 0
for ln, line in enumerate(lines, 1):
    depth_at[ln] = d          # 记录**行首**的深度
    for ch in line:
        if ch == "{":
            d += 1
        elif ch == "}":
            d = max(0, d - 1)


def find_blocks(selector):
    out = []
    for m in re.finditer(re.escape(selector) + r"\s*\{", t):
        ln = t[: m.start()].count("\n") + 1
        st = m.end() - 1
        dd, i = 0, st
        while i < len(t):
            if t[i] == "{":
                dd += 1
            elif t[i] == "}":
                dd -= 1
                if dd == 0:
                    break
            i += 1
        out.append((ln, depth_at.get(ln, -1), t[st : i + 1]))
    return out


root_body = block_body(":root")
if root_body is None:
    print("!! 找不到 :root 块")
    sys.exit(1)
root_vars = set(re.findall(r"(--[a-z0-9-]+)\s*:", root_body))

def top_level_vars(body):
    """只取块**直属**的变量声明（深度 1），不含嵌套规则里的。

    这一层区分是必须的：深色补丁块里有
        .table-light { --bs-table-bg: ...; --bs-table-color: ...; }
    这两个是**规则内部的局部变量**，作用域只在 .table-light 上，
    不是深色主题的全局变量，不该被算进「需要在 print 里复位」的清单。
    （第一版没做这个区分，把它们报成漏网，是假阳性。）
    """
    out = []
    d = 0
    for i, ch in enumerate(body):
        if ch == "{":
            d += 1
        elif ch == "}":
            d -= 1
        elif ch == ":" and d == 1:
            # 这个冒号属于哪个变量？先截到上一个语句分隔符，再取名字。
            # ⚠ 不能简单地「取前面最后一个 --name:」：
            #   同一行里写两个声明（--on-gold: #fff;  --on-gold-bright: #0a0a0a;）
            #   时，第二个冒号之前只有一个已完成的 `--name:`，
            #   结果它会被算成前一个变量，于是「已复位」误报成漏网。
            cut = max(body.rfind(";", 0, i), body.rfind("{", 0, i),
                      body.rfind("}", 0, i))
            m = re.search(r"(--[a-z0-9-]+)\s*$", body[cut + 1 : i])
            if m:
                out.append(m.group(1))
    return set(out)


dark_blocks = find_blocks(DARK)
print('=== [data-bs-theme="dark"] 共 %d 块 ===' % len(dark_blocks))
main_dark_vars = set()
print_vars = None
for ln, depth, body in dark_blocks:
    vs = top_level_vars(body)
    inside_media = depth >= 1
    print("  行 %5d  深度 %d  直属变量 %2d  %s"
          % (ln, depth, len(vs), "← @media print 兜底块" if inside_media else "← 顶层"))
    if inside_media:
        print_vars = vs
    else:
        # 必须**累加**：深色主题的变量分散在三个顶层块里
        # （主变量块、color-scheme、组件补丁），只留最后一个的话
        # 主变量块里的 60 个会被整体漏掉，检查就变成永远通过。
        main_dark_vars |= vs

if print_vars is None:
    print("!! 找不到 @media print 里的兜底块 —— 深色下打印会印出整页深底")
    sys.exit(1)

# 深色块里有、而 :root 里没有的变量 = 只在深色存在的变量，
# 必须由 print 块复位，否则打印时会漏出来。
only_dark = sorted(v for v in main_dark_vars if v not in root_vars)
print()
print("=== 只在深色块定义的变量 %d 个 ===" % len(only_dark))
missing = []
for v in only_dark:
    ok = v in print_vars
    print("  %-28s %s" % (v, "已复位" if ok else "!! 打印时会漏出来"))
    if not ok:
        missing.append(v)

# 反向：print 块复位了 :root 里根本没有的变量 → 写错了名字，永远不生效
extra = sorted(v for v in print_vars if v not in root_vars and v not in main_dark_vars)
print()
print("=== print 块里写了但 :root / 深色块都没有的变量 %d 个 ===" % len(extra))
for v in extra:
    print("  %-28s !! 无效声明：这个名字在别处从没定义过" % v)

print()
bad = missing + extra
if bad:
    print("!! %d 处需要修" % len(bad))
    sys.exit(1)
print("打印回退完整。")