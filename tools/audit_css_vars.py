"""列出 :root 与深色块各自定义了哪些变量，并报告**定义了却没人用**、
**用了却没人定义**的变量。

为什么要查这个
--------------
准备把深色主题从「深绿」换成「黑 + 古铜金」时，必须先知道色变量的全集。
如果某个变量只写在 print 回退块里、没写进真正的深色块，
那么深色模式下它会**静默回落到 :root 的浅色值** ——
页面能开、测试可能还过，但深色下某些文字/边框是浅色的。

本轮要回答的具体问题：
    深色块是否定义了 --bg / --text / --text-2 / --border / --border-strong
"""
from __future__ import annotations

import io
import re
from collections import Counter

PATH = "app/static/css/style.css"
s = io.open(PATH, encoding="utf-8").read()

# ---- 剥掉注释，避免注释里的 --var: 干扰统计 ----
def strip_comments(t):
    out = []
    i = 0
    n = len(t)
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


def block_vars(selector):
    """取出紧跟 selector 的第一个 { ... } 块内的变量名。"""
    m = re.search(re.escape(selector) + r"\s*\{", code)
    if not m:
        return None, None
    start = m.end() - 1
    d = 0
    i = start
    while i < len(code):
        if code[i] == "{":
            d += 1
        elif code[i] == "}":
            d -= 1
            if d == 0:
                break
        i += 1
    body = code[start : i + 1]
    return re.findall(r"(--[a-z0-9-]+)\s*:", body), body


def block_at_line(line_no):
    lines = code.split("\n")
    idx = line_no - 1
    # 原文件行号 -> 去注释后行号可能变；用选择器匹配更稳
    return None


root_vars, root_body = block_vars(":root")
dark_sel = '[data-bs-theme="dark"]'
# 深色块有两个：一个在 print 里，一个在顶层。取第一个顶层匹配的。
dm = list(re.finditer(re.escape(dark_sel) + r"\s*\{", code))
print("=== %s 出现 %d 次 ===" % (dark_sel, len(dm)))

dark_blocks = []
for m in dm:
    start = m.end() - 1
    d = 0
    i = start
    while i < len(code):
        if code[i] == "{":
            d += 1
        elif code[i] == "}":
            d -= 1
            if d == 0:
                break
        i += 1
    body = code[start : i + 1]
    dark_blocks.append((m.start(), body))

for pos, body in dark_blocks:
    vs = re.findall(r"(--[a-z0-9-]+)\s*:", body)
    nrule = len(re.findall(r"[^{}]+\{[^{}]*\}", body)) - len(vs)
    print("  偏移 %6d  变量 %2d  非变量规则 %2d" % (pos, len(vs), nrule))

dark_vars = set()
for _, b in dark_blocks:
    dark_vars |= set(re.findall(r"(--[a-z0-9-]+)\s*:", b))

print()
print("=== :root 定义 %d 个变量 ===" % len(root_vars or []))

SUSPECT = ["--bg", "--text", "--text-2", "--border", "--border-strong",
           "--surface", "--surface-2", "--primary-ink", "--primary-text",
           "--accent", "--head-bg", "--head-fg", "--zebra",
           "--card-head-bg", "--card-head-fg", "--card-head-border",
           "--card-head-muted", "--card-head-solid", "--topbar-bg",
           "--sidebar-bg", "--sidebar-text", "--sidebar-active",
           "--success-ink", "--warning-ink", "--danger-ink", "--info-ink",
           "--success-bg", "--warning-bg", "--danger-bg", "--info-bg"]

print()
print("%-24s %-10s %-10s %s" % ("变量", ":root 有", "深色块有", "被引用次数"))
print("-" * 60)
for v in SUSPECT:
    in_root = "是" if v in (root_vars or []) else "—"
    in_dark = "是" if v in dark_vars else "—"
    uses = len(re.findall(r"var\(\s*" + re.escape(v) + r"\s*[,)]", code))
    flag = ""
    if uses > 0 and v not in dark_vars and v != "--bg":
        flag = "   <== 深色模式下会回落到浅色 :root 值"
    if v == "--bg":
        flag = "   (仅 print 块有)" if v not in dark_vars else ""
    print("%-24s %-10s %-10s %3d%s" % (v, in_root, in_dark, uses, flag))

# ---- 全局：定义了但没人用 / 用了但没人定义 ----
defined = set(root_vars or []) | dark_vars
used = set(re.findall(r"var\(\s*(--[a-z0-9-]+)", code))
print()
print("=== 用了但没人定义（会继承浏览器默认/继承父级）===")
orphan_use = sorted(used - defined)
if orphan_use:
    for v in orphan_use:
        print("  %-28s 被引用 %d 次" % (v, len(re.findall(r"var\(\s*" + re.escape(v) + r"\s*[,)]", code))))
else:
    print("  （无）")

print()
print("=== 定义了但没人用（死变量，可能是上一轮换色的残留）===")
cnt = Counter(re.findall(r"var\(\s*(--[a-z0-9-]+)", code))
dead = [v for v in sorted(defined) if cnt.get(v, 0) == 0]
if dead:
    for v in dead:
        print("  %s" % v)
else:
    print("  （无）")