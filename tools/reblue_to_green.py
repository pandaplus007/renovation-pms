"""把界面从蓝色调回绿色调，深色主题保留。

背景
----
曾经按用户要求把主色换成蓝色 #3E6CA8，结论是「不如绿色」，改回来。
但**不是整个回滚**——本轮同时做了三件与色相无关的事，它们全部保留：

  1. 排版刻度（12/14/16/20/24/28 六档 + 字重两档 + 字距）
     这是用户最初要解决的问题（"文字大小有些大有些小"），
     与主色是什么颜色毫无关系。
  2. 深色模式：明确要求保留。
  3. 一批结构性修复：.small/.form-text/`<small>` 的 em 级联、
     .fw-bold 的 700、表头硬写 14px 盖掉刻度、进度条数字偏上 3.2px。

所以这里是**按色相反转**，不是恢复备份。

做法
----
1. 中性色阶与主色不用反转，直接用 tools/build_palette.py 按绿色相重新算
   ——那份是**按对比度目标反解**出来的，精确且可复算。反转蓝色得到的
   绿色只是「明度相同」，不保证落在刻度与对比度上。
2. 其余散落的硬编码色值用「保留明度、色相转绿」批量反转——
   这些是装饰色/渐变端点，不需要逐个验对比度。
3. 莫兰迪强调色**直接还原成原值**：燕麦/藕粉/陶土/豆沙绿本来就是
   围绕绿调一起设计的，换蓝时它们失去了依托，现在该回到原样。

排除：成功语义色（绿=已完成）本来就该是绿的，不动。
"""
from __future__ import annotations

import colorsys
import io
import re

PATH = "app/static/css/style.css"
TARGET_HUE = 157.4          # #3d7a63 的色相
OLD_HUE_LO, OLD_HUE_HI = 190.0, 245.0   # 蓝色相区间

# 成功语义色：本来就是绿的，不参与反转
KEEP = {
    "#10b981",  # --success
    "#1d6b52",  # --success-ink
    "#34d399",  # 深色主题 --success-ink
    "#e7f4ef",  # --success-bg
    "#14332a",  # 深色主题 --success-bg
}

# 莫兰迪强调色：还原成设计时的原值（换蓝时被改成了冷调）
MORANDI = {
    "--m-oat":   "#cbb392",   # 燕麦
    "--m-lotus": "#b98ea3",   # 藕粉
    "--m-terra": "#c07c6a",   # 陶土橙红
    "--m-bean":  "#7d9b7a",   # 豆沙绿
    "--m-moss":  "#3d7a63",   # = 主色
    "--m-sage":  "#8aa79a",   # 灰绿
    "--m-stone": "#9aa39c",   # 石灰
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


def to_green(hexs: str) -> str:
    h = hexs.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    _, l, s = colorsys.rgb_to_hls(r, g, b)
    ns = min(1.0, s * 1.06) if s > 0.02 else s
    nr, ng, nb = colorsys.hls_to_rgb(TARGET_HUE / 360.0, l, ns)
    return "#%02x%02x%02x" % (round(nr * 255), round(ng * 255), round(nb * 255))


src = io.open(PATH, encoding="utf-8").read()

# 字符级掩码：只改活代码，注释里写的色值是「来由记录」，要留着
in_comment = [False] * len(src)
i, n, c = 0, len(src), False
while i < n:
    if not c and src.startswith("/*", i):
        c = True
        in_comment[i] = in_comment[i + 1] = True
        i += 2
        continue
    if c and src.startswith("*/", i):
        in_comment[i] = in_comment[i + 1] = True
        c = False
        i += 2
        continue
    in_comment[i] = c
    i += 1

changes = []


def repl(m):
    if in_comment[m.start()]:
        return m.group(0)
    h = "#" + m.group(1).lower()
    if h in KEEP:
        return m.group(0)
    r = int(m.group(1)[0:2], 16)
    g = int(m.group(1)[2:4], 16)
    b = int(m.group(1)[4:6], 16)
    hu = hue_of(r, g, b)
    if hu is None or not (OLD_HUE_LO <= hu <= OLD_HUE_HI):
        return m.group(0)
    new = to_green(h)
    ln = src.count("\n", 0, m.start()) + 1
    changes.append((ln, h, new))
    return new


out = re.sub(r"#([0-9a-fA-F]{6})\b", repl, src)

# ---- 莫兰迪强调色还原 ----
mor = []
for name, val in MORANDI.items():
    pat = re.compile(re.escape(name) + r":\s*#[0-9a-fA-F]{6}")
    m = pat.search(out)
    if not m:
        continue
    old = m.group(0)
    out = pat.sub("%s: %s" % (name, val), out, count=1)
    mor.append((name, old.split(":")[-1].strip(), val))

print("=" * 74)
print("蓝调 → 绿调：反转 %d 处色值" % len(changes))
print("=" * 74)
for ln, old, new in changes[:12]:
    print("  行 %5d  %s -> %s" % (ln, old, new))
if len(changes) > 12:
    print("  … 另有 %d 处" % (len(changes) - 12))

print()
print("莫兰迪强调色还原 %d 项：" % len(mor))
for name, old, new in mor:
    print("  %-12s %s -> %s" % (name, old, new))

io.open(PATH, "w", encoding="utf-8", newline="").write(out)
print()
print("已写入 %s" % PATH)
print()
print("※ 中性色阶与主色还没换——下一步用 build_palette 的产出覆盖 :root 与深色块。")