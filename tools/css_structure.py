"""CSS 结构分析：找出每个选择器块**真正**归属的 @media。

为什么不能用「向上找最近的 @media 行」：
    CSS 注释里出现的 @media 字样会骗过这种写法；更常见的是嵌套 @media
    （@media print 里再嵌 @media），单层扫描会把内层算到外层头上。
    本次就踩了：行 574 的深色主题块被误判成「在 @media print 里」，
    而它其实是顶层规则。判据必须是花括号配平。

只做分析，不改文件。
"""
from __future__ import annotations

import io
import re
import sys

PATH = "app/static/css/style.css"
s = io.open(PATH, encoding="utf-8").read()
lines = s.split("\n")

# 逐字符扫描，维护 (深度, 该深度上开启的 at-rule 描述)
frames = []          # 栈：每项 = 开括号时所处的 at-rule 名，None 表示非 at-rule
line_depth = {}      # 行号 -> 该行**开始时**的深度
line_owner = {}      # 行号 -> 该行开始时最内层 at-rule 名

depth = 0
owner = None
for ln, line in enumerate(lines, 1):
    line_depth[ln] = depth
    line_owner[ln] = owner
    for ch in line:
        if ch == "{":
            # 这一行开括号之前的内容里，若末尾是 @xxx 则该块是 at-rule
            pre = line[: line.index(ch)] if ch in line else ""
            frames.append(owner)
            m = re.search(r"(@[a-z-]+[^{]*)$", pre)
            owner = m.group(1).strip() if m else None
            depth += 1
        elif ch == "}":
            if frames:
                owner = frames.pop()
            depth = max(0, depth - 1)

print("=== 所有 [data-bs-theme=\"dark\"] 块的真实归属 ===")
for ln, line in enumerate(lines, 1):
    if not re.match(r'\s*\[data-bs-theme="dark"\]\s*\{', line):
        continue
    om = line_owner.get(ln)
    om = om if om else "（顶层）"
    # 统计块里的变量数
    d = line_depth[ln]
    nvar = nrule = 0
    end = ln
    dd = d
    for k in range(ln, len(lines) + 1):
        dd = line_depth.get(k, dd)
        if k > ln and dd <= d:
            end = k
            break
    body = "\n".join(lines[ln - 1 : end])
    nvar = len(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", body, re.M))
    nrule = len(re.findall(r"^\s*[.#a-zA-Z\[][^{]*\{", body, re.M))
    print("  行 %5d-%-5d  深度 %d  归属 %-34s  变量 %2d  规则 %3d"
          % (ln, end, d, om, nvar, nrule))

print()
print("=== 顶层 @media 块清单（按深度 1 找）===")
for ln, line in enumerate(lines, 1):
    m = re.match(r"\s*(@media[^{]*)\{", line)
    if m and line_depth.get(ln) == 0:
        print("  行 %5d  %s" % (ln, m.group(1).strip()[:70]))