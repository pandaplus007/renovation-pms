"""把模板里的内联 font-size 归到刻度上。

为什么不用 rem/pt
----------------
`.72rem` / `.85rem` / `2.5rem` 这类写法的问题是**换算要靠心算**：
html 默认 16px，0.72rem = 11.52px —— 又是刻度外的尺寸。
统一成 px 之后，读代码就知道实际是多少，不必先乘一遍。

`todo.html` 的 `2.5rem`（=40px）是空状态大标题，属于展示型字号，
刻度里最接近的是 --fs-5 (28px)。往下压一档换来与整页层级一致，
不值得为此在刻度外开一个 40px。

**打印页（print.html）例外**：纸质签字单用 pt 是对的——
pt 是纸面单位，中文公文的五号字就是 10.5pt。屏幕用 px、纸面用 pt，
这两套单位不统一是有道理的，不要在这里"顺手统一"。
"""
from __future__ import annotations

import io
import re
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 内联字号 → 刻度
SIZE_MAP = {
    "12px": "12px",
    "14px": "14px",
    # 11px 是全文最小的字，「已付清」这类角标都靠它。
    # Carbon 最小档就是 12px，11px 中文在 Windows 上笔画已经开始粘连，
    # 与其留一档刻度外的字，不如让它并到 12px。
    "11px": "12px",
    "11.5px": "12px",
    ".72rem": "12px",
    ".7rem": "12px",
    ".76rem": "12px",
    "0.7rem": "12px",
    "0.85rem": "12px",
    ".85rem": "12px",
    ".8rem": "12px",
    ".9rem": "14px",
    "0.9rem": "14px",
    ".98rem": "16px",
    "13px": "14px",
    "2.5rem": "28px",
    # 已经是刻度内的，原样保留
    "16px": "16px",
    "20px": "20px",
    "24px": "24px",
    "28px": "28px",
}

# 这些文件用 pt 是刻意的（纸面）
SKIP = {"changes/print.html"}

changes = []
for f in sorted(glob.glob(os.path.join(ROOT, "app", "templates", "**", "*.html"),
                          recursive=True)):
    rel = os.path.relpath(f, os.path.join(ROOT, "app", "templates")).replace("\\", "/")
    if rel in SKIP:
        continue
    src = io.open(f, encoding="utf-8").read()
    out_lines = []
    touched = [False]
    for ln_no, line in enumerate(src.split("\n"), 1):
        # 注释与 <script>/<style> 里的字号不在这轮范围内
        if re.match(r"\s*(#|\{|//|\*|/\*)", line) or "<script" in line or "<style" in line:
            out_lines.append(line)
            continue

        def sub(m, _ln=ln_no, _line=line):
            old = m.group(1) + m.group(2)
            new = SIZE_MAP.get(old)
            if new and new != old:
                touched[0] = True
                changes.append((rel, _ln, old, new, _line.strip()[:50]))
                return "font-size:%s" % ((m.group(1) + new) if m.group(1) else new)
            return m.group(0)

        out_lines.append(re.sub(r"font-size:(\s*)([0-9.]+(?:px|rem|pt|em))", sub, line))
    if touched:
        io.open(f, "w", encoding="utf-8", newline="").write("\n".join(out_lines))

print("=" * 74)
print("内联字号归一（%d 处）" % len(changes))
print("=" * 74)
for rel, ln, old, new, ctx in changes:
    print("  %-28s %5d  %-8s -> %-8s  %s" % (rel, ln, old, new, ctx))

# ---- 自检：非打印页不应再有刻度外字号 ----
print()
print("=" * 74)
print("自检")
print("=" * 74)
left = []
for f in sorted(glob.glob(os.path.join(ROOT, "app", "templates", "**", "*.html"),
                          recursive=True)):
    rel = os.path.relpath(f, os.path.join(ROOT, "app", "templates")).replace("\\", "/")
    if rel in SKIP:
        continue
    t = io.open(f, encoding="utf-8").read()
    # 去掉 Jinja 注释再找：注释里常常出现「14px」这样的说明
    t = re.sub(r"\{#.*?#\}", "", t, flags=re.S)
    for m in re.finditer(r"font-size:\s*([0-9.]+)(px|rem|pt|em)", t):
        v, u = float(m.group(1)), m.group(2)
        if u != "px" or v not in (12, 14, 16, 20, 24, 28):
            ln = t[:m.start()].count("\n") + 1
            left.append((rel, ln, m.group(0)))
if left:
    print("  刻度外字号 %d 处：" % len(left))
    for rel, ln, txt in left:
        print("    %-28s %5d  %s" % (rel, ln, txt))
else:
    print("  非打印页的字号全部落在 12/14/16/20/24/28 六档内")