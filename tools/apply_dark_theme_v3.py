"""把深色主题从 v2（金色）整体切到 v3（蓝绿）。

为什么必须用脚本而不是一串 edit
------------------------------
这次要改的地方散在四个不同的位置，而且**互相有依赖**：
    1. 主变量块里的强调色与文字档
    2. 主变量块里「压在强调色上的文字色」（方向要跟着换）
    3. 组件补丁块里引用 --gold-* 的地方（变量已改名成 --accent-*）
    4. print 回退块里同样的变量名
漏掉任何一处的表现都是「深色下某个元素还是金色」，而对比度测试发现不了
（因为金色与蓝绿在黑底上都够亮）。

方向性的一处反转，值得单独记
----------------------------
金色亮度 0.12~0.16（偏暗）→ 满格底上压**白**字
蓝绿亮度 0.372（偏亮）    → 满格底上压**近黑**字
同一条规则「底越亮字越暗」，v3 的强调色落在另一侧。
所以 --on-accent / --on-accent-bright 两个变量不是冗余，
而是「换强调色那一轮必然会用到」的东西。
"""
from __future__ import annotations

import io
import re
import shutil
import sys

PATH = "app/static/css/style.css"
shutil.copyfile(PATH, PATH + ".bak-v3")
print("已备份 -> %s.bak-v3" % PATH)

s = io.open(PATH, encoding="utf-8").read()
before = s

# ---------------------------------------------------------------- 1. 变量改名
# --gold-deep -> --accent-deep 等。名字必须改：留着 gold 会让下一个读代码的
# 人以为深色主题还是金色（本轮已经有过一次「注释写着金色、值是青色」的混乱）。
RENAME = [
    ("--gold-deep", "--accent-deep"),
    ("--gold-bar", "--accent-bar"),
    ("--on-gold-bright", "--on-accent-bright"),
    ("--on-gold", "--on-accent"),
]
print()
print("=== 1. 变量改名（金 -> accent）===")
for old, new in RENAME:
    n = len(re.findall(r"(?<![\w-])" + re.escape(old) + r"(?![\w-])", s))
    # 先替换更长的名字，避免 --on-gold 先被 --on-gold-bright 吃掉
    s = re.sub(r"(?<![\w-])" + re.escape(old) + r"(?![\w-])", new, s)
    print("  %-18s -> %-20s %2d 处" % (old, new, n))

# ---------------------------------------------------------------- 2. 值替换
# 只在**主变量块**内替换值（组件补丁块里已经是 var() 引用，不该有字面值）。
print()
print("=== 2. 主变量块内的色值 ===")
VALUES = [
    # 强调色
    ("--accent: #c9a227", "--accent: #14B8A6"),
    ("--primary-ink: #c9a227", "--primary-ink: #47a69c"),
    # 选中态 / 进度条 / 满格
    ("--accent-deep: #7f6925", "--accent-deep: #28776f"),
    ("--accent-bar: #997c26", "--accent-bar: #298e83"),
    # 文字档
    ("--gray-400:#5d5c59", "--gray-400:#5b5c5f"),
    ("--gray-500:#777570", "--gray-500:#73757a"),
    ("--gray-600:#918f88", "--gray-600:#9CA3AF"),
    ("--gray-700:#a8a6a1", "--gray-700:#a4a6ab"),
    ("--gray-800:#CBD5E1", "--gray-800:#d3d4d7"),
    ("--gray-900:#F1F5F9", "--gray-900:#E5E7EB"),
    # rgb 三元组
    ("--bs-emphasis-color-rgb: 241, 245, 249", "--bs-emphasis-color-rgb: 229, 231, 235"),
    ("--bs-secondary-color-rgb: 145, 143, 136", "--bs-secondary-color-rgb: 156, 163, 175"),
    ("--bs-tertiary-color-rgb: 119, 117, 112", "--bs-tertiary-color-rgb: 115, 117, 122"),
    # 链接
    ("--bs-link-color: #c9a227", "--bs-link-color: #47a69c"),
    ("--bs-link-hover-color: #e0b93a", "--bs-link-hover-color: #88d3cb"),
    ("--bs-link-color-rgb: 201, 162, 39", "--bs-link-color-rgb: 71, 166, 156"),
    ("--bs-link-hover-color-rgb: 224, 185, 58", "--bs-link-hover-color-rgb: 136, 211, 203"),
    # 渐变（主按钮 / tab / 进度条）
    ("linear-gradient(135deg, #7a5c1a 0%, #8a6f22 100%)",
     "linear-gradient(135deg, #28776f 0%, #67beb5 100%)"),
    ("linear-gradient(135deg, #8a6f22 0%, #8f7426 100%)",
     "linear-gradient(135deg, #67beb5 0%, #298e83 100%)"),
    # 标签（表格里的项目名徽章）
    ("--tag-bg:        rgba(201, 162, 39, .14)", "--tag-bg:        rgba(20, 184, 166, .14)"),
    ("--tag-bg-hover:  rgba(201, 162, 39, .24)", "--tag-bg-hover:  rgba(20, 184, 166, .24)"),
    ("--tag-fg:        #dcbb52", "--tag-fg:        #5fd4c8"),
    ("--tag-border:    rgba(201, 162, 39, .30)", "--tag-border:    rgba(20, 184, 166, .30)"),
    # 侧栏选中
    ("--sidebar-active: linear-gradient(90deg, #5c4a1c, #7f6925)",
     "--sidebar-active: linear-gradient(90deg, #28776f, #2f8a81)"),
    # 输入框描边
    ("--input-border: #404040", "--input-border: #404040"),
]
missed = []
for old, new in VALUES:
    if old in s:
        n = s.count(old)
        s = s.replace(old, new)
        print("  %-52s %2d 处" % (old[:52], n))
    else:
        missed.append(old)
if missed:
    print()
    print("  !! 以下 %d 条没找到（可能上一轮已改或写法不同）：" % len(missed))
    for m in missed:
        print("     %s" % m)

# ---------------------------------------------------------------- 3. 文字色方向
print()
print("=== 3. 满格强调色上的文字色（方向反转：金→白字，蓝绿→近黑字）===")
# --on-active-grad 在 v2 是白字（压金渐变），v3 必须改成近黑字（压青绿渐变）
n = len(re.findall(r"--on-active-grad:\s*#ffffff", s))
s = re.sub(r"(--on-active-grad:\s*)#ffffff", r"\1#0a0a0a", s)
print("  --on-active-grad  #ffffff -> #0a0a0a   %d 处" % n)

# --on-accent（压 --accent-deep 的白字）保持白字
# --on-accent-bright 压 --accent-full 的近黑字）保持近黑
# 这两个不需要改，但要在报告里说明它们**没改**是有原因的。

# ---------------------------------------------------------------- 4. print 回退块
print()
print("=== 4. print 回退块 ===")
# 深色独有的变量必须在 @media print 里复位。v2 复位成浅色的绿值，
# v3 换强调色后这几行也要跟着换（名字已由步骤 1 改过，值在这里改）。
PRINT_VALUES = [
    ("--accent-deep: #8a6720", "--accent-deep: #2f6a55"),
    ("--accent-bar: #bf8a22", "--accent-bar: #3f8168"),
    ("--on-accent-bright: #0a0a0a", "--on-accent-bright: #ffffff"),
    ("--on-accent: #ffffff", "--on-accent: #ffffff"),
]
for old, new in PRINT_VALUES:
    if old in s:
        n = s.count(old)
        s = s.replace(old, new)
        print("  %-40s %2d 处" % (old, n))
    else:
        print("  跳过（未找到）：%s" % old)

io.open(PATH, "w", encoding="utf-8", newline="").write(s)

print()
print("=" * 66)
print("写入完成")
print("  行数 %d -> %d" % (before.count("\n"), s.count("\n")))
print("  花括号配平：%d（应为 0）" % (s.count("{") - s.count("}")))
print("  CSS 注释配平：%d（应为 0）" % (s.count("/*") - s.count("*/")))
print("  乱码字符：%d（应为 0）" % s.count("\ufffd"))
print()
print("残留的金色字面值（应只剩注释与浅色主题）：")
for c in ("#c9a227", "#e0b93a", "#dcbb52", "#7f6925", "#997c26", "#7a5c1a", "#8a6f22"):
    n = len(re.findall(re.escape(c), s, re.I))
    if n:
        print("  %-9s %d 处" % (c, n))