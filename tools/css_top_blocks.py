"""列出 CSS 里的顶层块（只到深度 1），供定位用。

为什么需要它
------------
`[data-bs-theme="dark"]` 在 style.css 里出现 37 次**开括号**，
但只有 4 个是真正的顶层块（其余都在嵌套规则里，比如
`.table a[href^="/projects/"]:not(...)` 这种选择器本身就带 `[...]`）。
我原来用「行首是 `[data-bs-theme=`」来找顶层块，把选择器里带方括号的
那些也算进去了 —— 于是「最后一个块」选到的是某个 `.table a[...]` 规则，
不是组件补丁块。安全断言正确地拦下了这次错误插入。

判据必须是**花括号深度 == 0**，不是文本匹配。
"""
from __future__ import annotations

import io
import re
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "app/static/css/style.css"
s = io.open(PATH, encoding="utf-8").read()
lines = s.split("\n")

depth = 0
depth_at = {}
for i, line in enumerate(lines, 1):
    depth_at[i] = depth
    for ch in line:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)


def block_end(start):
    d = 0
    for i in range(start, len(lines) + 1):
        for ch in lines[i - 1]:
            if ch == "{":
                d += 1
            elif ch == "}":
                d -= 1
                if d == 0:
                    return i
    return None


print("=== 顶层块（深度 0 起始）===")
for i, line in enumerate(lines, 1):
    if depth_at.get(i, -1) != 0:
        continue
    t = line.strip()
    if not t.endswith("{") and "{" not in t:
        continue
    sel = t.split("{")[0].strip()
    if not sel:
        continue
    en = block_end(i)
    size = (en - i + 1) if en else -1
    # 只列「真正是规则开头」的：前面不是逗号续行
    if t.startswith(","):
        continue
    print("  行 %5d-%-5s (%3d 行)  %s"
          % (i, en if en else "?", size, sel[:78]))