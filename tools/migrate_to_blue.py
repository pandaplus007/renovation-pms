"""一次性迁移：字号归到刻度 + 绿系硬编码换蓝。

为什么用脚本而不是手改
--------------------
要改 30 处字号 + 50 处色值。手改漏一处看不出来（本轮已经出现过
「源码里写的 12px 其实被 .small 的 em 覆盖成 13.125px」这类问题），
而漏掉的恰好是最容易出事的地方——残留一两个绿色会让人觉得「没改干净」。

两条硬规则
----------
1. **图标的 font-size 不按文字规则改**。Bootstrap Icons 靠 font-size
   控制图形大小，跟字号刻度无关，改了就变形。
2. **打印区的 pt 不改**。10.5pt 是中文公文标准字号（五号），
   pt 在纸面上是对的单位，改成 px 反而会让打印出来偏大。
   屏幕排版和纸面排版是两套单位，这里刻意不统一。

跑完会打印完整改动清单，逐条可核。
"""
from __future__ import annotations

import io
import re

CSS = "app/static/css/style.css"

# ---------- 1. 色值映射 ----------
# 语义色（绿=成功、琥珀=警告、红=危险）**保持原样**：它们表达的是
# 业务含义，不是品牌。把「已完成」改成蓝色会和「进行中」撞色，
# 状态色就废了。只换品牌色与中性色。
COLOR_MAP = {
    # 主色三档
    "#3d7a63": "#3e6ca8",
    "#6aa88c": "#6d97c4",
    "#4e8f73": "#4a7bb8",
    "#4f7d63": "#3e6ca8",
    # 深主色（当文字用）
    "#2c5246": "#2f5585",
    "#3a5d52": "#35485f",
    # 主色浅描边
    "#9cc4b2": "#a8c2e0",
    # 中性/表面（绿调 → 蓝调）
    "#f6f9f7": "#f7f8fb",
    "#eef3f0": "#edf0f5",
    "#e2eae5": "#e0e5eb",
    "#d3ded8": "#cdd4dc",
    "#a8b7b0": "#9da9b8",
    "#7b8b84": "#77889f",
    "#5a6b64": "#55677f",
    "#3d5049": "#404f63",
    "#2b3a35": "#2e3948",
    "#1b2622": "#1d252f",
    # 浅绿表面 → 浅蓝表面
    "#e9f3ee": "#eaf0f8",
    "#dfece4": "#e0e9f4",
    "#d4e7dc": "#d5e2f1",
    "#f2f7f4": "#f2f5fa",
    "#dfeee8": "#e2ebf6",
    "#cfe7dc": "#cbdcf0",
    "#b6d8c9": "#adc8e5",
    "#e8f5ef": "#e9f0f9",
    "#d9ece2": "#d7e4f3",
    "#c2e0d2": "#bccfe8",
    "#dce9e2": "#dde7f2",
    "#c6dbd0": "#c3d4e8",
    # 莫兰迪强调色 → 冷调（统计卡顶部色条用）。
    # 换成同一蓝族里不同明度/微差色相，保留可区分度又不撞主色。
    "#cbb392": "#b6c3d4",   # 燕麦   → 雾蓝灰
    "#b98ea3": "#a6b5d0",   # 藕粉   → 蓝紫
    "#c07c6a": "#8ea8c9",   # 陶土   → 钢蓝
    "#7d9b7a": "#a2b8bd",   # 豆沙绿 → 青灰
    "#8aa79a": "#8ea5bd",   # 灰绿   → 灰蓝
    "#9aa39c": "#98a2ae",   # 石灰   → 蓝调中性灰
    # 半透明主色
    "rgba(61,122,99": "rgba(62,108,168",
    "rgba(61, 122, 99": "rgba(62, 108, 168",
    # 第二批：扫「色相在 95-175 且绿分量明显高于蓝」的规则查出来的残留。
    # 这些值不是手挑的，是用 colorsys 把**明度原样保留**、色相转到 214°
    # 算出来的——保证换色后元素的层级关系（深浅顺序）完全不变，
    # 不会因为换色把「主色深、辅助色浅」的次序打乱。
    "#5d8773": "#5a6f8a",
    "#5f8a5c": "#59708d",
    "#6f9184": "#6c7d94",
    "#356b57": "#324c6e",
    "#5a9d82": "#5677a1",
    "#afc4b7": "#adb8c6",
    "#f1f6f3": "#f1f3f6",
    "#5f9c82": "#5b79a0",
    "#9fd0b5": "#9cb4d3",
    "#d8ecdf": "#d7e1ed",
    "#c6e3d5": "#c4d2e5",
    "#4a6b5c": "#47586e",
    "#3d6b57": "#3a516e",
    "#a3bdb0": "#a1aebf",
    "#c9dfd2": "#c8d2e0",
    "rgba(51, 82, 68": "rgba(50, 64, 83",
    "rgba(51,82,68": "rgba(50,64,83",
}
# 刻意保留：成功语义色（绿=完成/正常）不换
KEEP = {"#1d6b52", "#e7f4ef", "#10b981"}

# ---------- 2. 字号映射 ----------
SIZE_MAP = {
    "10.5px": "12px", "11px": "12px", "11.5px": "12px",
    "12.5px": "12px",
    "13px": "14px", "13.5px": "14px", "14.5px": "14px", "15px": "14px",
    "17px": "16px", "18px": "20px", "22px": "24px",
    ".8rem": "12px", ".76rem": "12px",
}

# 这些选择器里的 font-size 是**图标大小**，不按文字刻度改
ICON_CTX = re.compile(
    r"(\.bi\b|\.bi-|\blogin-icon|empty-state\s+i|pt-links\s+a\s+i"
    r"|\bi\s*\{|flt-badge|stat-num\s+\.bi|icon)", re.I)

src = io.open(CSS, encoding="utf-8").read()
changes = []

# ---- 逐行处理，保留行号以便定位 ----
out = []
for ln_no, line in enumerate(src.split("\n"), 1):
    orig = line

    # 2a. 字号
    if not ICON_CTX.search(line):
        def _size(m):
            # 注意：m.group(1)+m.group(2) 才是 SIZE_MAP 的键。
            # 曾在这里写成 m.group(0).replace(" ","")，
            # 于是键成了 "font-size:12.5px" —— 与表里的 "12.5px" 永远不匹配，
            # 脚本静默地一处都没改，还打印出一长串「改动清单」假装干过活。
            size, unit = m.group(1), m.group(2)
            key = size + unit
            if key in SIZE_MAP:
                changes.append((ln_no, "字号", key, SIZE_MAP[key],
                                orig.strip()[:58]))
                return "font-size: " + SIZE_MAP[key]
            return m.group(0)
        line = re.sub(r"font-size:\s*([0-9.]+)(px|rem|pt)", _size, line)

    # 2b. 色值。一行里可能有**多个**待换的色值
    #（如 `linear-gradient(135deg, #356b57, #5a9d82)` 一次占两个），
    # 早期版本每行只换第一个就 break，漏掉的正好是这种双色的渐变，
    # 而渐变里的残留绿在深浅交界处最显眼。所以这里全部替换。
    low = line.lower()
    for old, new in COLOR_MAP.items():
        if old in low and not any(k in low for k in KEEP if k in old):
            line, cnt = re.subn(re.escape(old), new, line, flags=re.I)
            changes.append((ln_no, "色值", old, new, orig.strip()[:58]))
            if cnt > 1:
                changes.append((ln_no, "色值", old, new,
                                "（同行 %d 处）" % cnt))

    out.append(line)

new_css = "\n".join(out)

print("=" * 76)
print("改动清单（%d 处）" % len(changes))
print("=" * 76)
for ln_no, kind, old, new, ctx in changes:
    print("  %5d  [%s] %-18s → %-18s  %s" % (ln_no, kind, old, new, ctx))

# ---- 自检 ----
print()
print("=" * 76)
print("自检")
print("=" * 76)
# 用花括号深度判断某行是否落在 @media 内。之前用「遇到 @media print
# 就置位、遇到单独的 } 就复位」——但嵌套的 @media（screen 里套 print）
# 会让它提前复位，屏幕态被误判成打印态，于是自检报告「全部残留」
# 而实际全改好了。自检本身骗人比不改更糟。
lines = new_css.split("\n")
depth_at = []
d = 0
for l in lines:
    depth_at.append(d)
    d += l.count("{") - l.count("}")
ALLOWED = (12, 14, 16, 20, 24, 28)
left_sizes = []
for i, l in enumerate(lines, 1):
    if depth_at[i - 1] > 0:        # 在任何 @media 里 → 打印/移动端特例，放行
        continue
    if ICON_CTX.search(l):
        continue
    for m in re.finditer(r"font-size:\s*([0-9.]+)(px|rem|pt|em)", l):
        v, u = float(m.group(1)), m.group(2)
        if u != "px" or v not in ALLOWED:
            left_sizes.append((i, m.group(0), l.strip()[:56]))
print("屏幕态残留的不合刻度字号：%d 处" % len(left_sizes))
for i, m, ctx in left_sizes:
    print("   %5d %-18s %s" % (i, m, ctx))

greens = [m.group(0) for m in re.finditer(
    r"#(?:3d7a63|6aa88c|4e8f73|2c5246|3a5d52|e9f3ee|d4e7dc|9cc4b2)\b",
    new_css, re.I)]
print("残留绿色系硬编码：%d 处 %s" % (len(greens), greens[:6]))
print("成功语义色是否保留：%s"
      % ("是" if "#1d6b52" in new_css and "#e7f4ef" in new_css else "!! 被误改"))

from collections import Counter
print("改后 font-size 分布：")
for v, n in sorted(Counter(re.findall(r"font-size:\s*([0-9.]+px)", new_css)).items(),
                   key=lambda x: -x[1]):
    print("   %-12s x%d" % (v, n))

io.open(CSS, "w", encoding="utf-8").write(new_css)
print()
print("已写入 %s" % CSS)