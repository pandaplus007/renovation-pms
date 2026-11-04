"""深色主题配色推导器：近黑底 + 白字 + 古铜金强调。

为什么不能直接改 build_palette.py 的主色参数
--------------------------------------------
build_palette.py 按**主色色相**生成整条中性色阶，底色也是算出来的
（hsl(H, .24, .10)）。而这一轮：
  · 底色由用户定死 #0a0a0a（近黑），不是算出来的
  · 中性色阶要**中性**（无色相），因为「黑色为主」本身就意味着去色
  · 品牌强调色另算：古铜金 #C9A227，绿色只留给成功态

所以这里固定底色，只反解各档明度。

明度必须反解，不能手挑
----------------------
WCAG 亮度权重是 0.2126 R / 0.7152 G / 0.0722 B。绿光权重最高，
蓝色最低——**同样的 HSL 明度，绿色看起来比蓝色亮得多**。
上一轮把这套明度参数从蓝搬到绿，一口气击穿 gray-700/600/500
（实测 6.41 / 4.33 / 2.82）。手挑必然重蹈。

而且这次底色比原来更黑（#0a0a0a vs #13201b，亮度 0.0033 vs 0.0100），
**同样的前景色，对比度会比原来更高**，所以不能凭上轮的数字直接搬——
必须每档重算。

用法：python tools/build_dark_gold_palette.py
输出：可粘贴的 [data-bs-theme="dark"] 变量块 + 全部对比度验算
"""
from __future__ import annotations

import sys


# ---------- 色彩工具（与 build_palette.py 同源，保证两套算出来的数可比） ----------
def srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hexstr: str) -> float:
    h = hexstr.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g)
            + 0.0722 * srgb_to_lin(b))


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hsl_to_hex(h: float, s: float, l: float) -> str:
    """h 取 0-360，s/l 取 0-1。"""
    c = (1 - abs(2 * l - 1)) * s
    hp = h % 360 / 60.0
    x = c * (1 - abs(hp % 2 - 1))
    if hp < 1:   r, g, b = c, x, 0
    elif hp < 2: r, g, b = x, c, 0
    elif hp < 3: r, g, b = 0, c, x
    elif hp < 4: r, g, b = 0, x, c
    elif hp < 5: r, g, b = x, 0, c
    else:        r, g, b = c, 0, x
    m = l - c / 2
    to = lambda v: max(0, min(255, round((v + m) * 255)))
    return "#%02x%02x%02x" % (to(r), to(g), to(b))


def solve_lightness(hue: float, sat: float, bg: str, need: float) -> str:
    """二分搜索明度使 对比度(bg) >= need，取**刚好达标**的那一档。

    深色主题：明度越低越暗、对比越低，达标集合是 L >= T，
    收敛方向与浅色主题**相反**——写反会一路跑到 0.98 再报「解不出」，
    而实际 L=0.5 时早就该有 8:1 了。
    """
    lo, hi = 0.02, 0.98
    best = None
    for _ in range(40):
        mid = (lo + hi) / 2
        if contrast(hsl_to_hex(hue, sat, mid), bg) >= need:
            best = mid
            hi = mid
        else:
            lo = mid
    if best is None:
        raise SystemExit("解不出：色相 %.0f° 饱和度 %.2f 对 %s 达到 %.1f:1"
                         % (hue, sat, bg, need))
    # 留余量，别正好卡在 WCAG 线上
    return hsl_to_hex(hue, sat, min(0.97, best + 0.012))


# ---------- 用户定死的底色 ----------
DARK_BG = "#0a0a0a"          # 近黑，不是纯黑：纯黑配纯白对比过冲、发涩
GOLD = "#c9a227"             # 古铜金，用户选定

# 中性色阶用极低饱和度的**暖灰**（色相取金的 40°，饱和度 ≤0.07）。
# 为什么不用纯中性（s=0）：纯灰死板；带一点点暖调能和金色强调呼应，
# 而 0.05 的饱和度在屏幕上基本读不出来，只在深色大面积上更耐看。
# 注意：surfaces 段（50~300）刻意保持 s=0 纯中性——那里是面积最大的地方，
# 一点偏色就会被读成「底色脏」。
GOLD_H = 40.0

DARK = {"gray-50": DARK_BG}     # 页面底：定死
for _n, _s, _need in [
        ("gray-100", 0.00, 1.10),   # 次级底 / surface-2
        ("gray-200", 0.00, 1.25),   # 常规边框（装饰）
        ("gray-300", 0.00, 1.60),   # 强边框 / 输入框描边
        ("gray-400", 0.03, 2.60),
        ("gray-500", 0.04, 3.40),   # 占位 / text-3（不承载关键信息）
        ("gray-600", 0.05, 5.00),   # 次要文字 text-2
        ("gray-700", 0.05, 7.50),   # 按钮文字 / 强文字
        ("gray-800", 0.06, 10.0),
        ("gray-900", 0.06, 12.0)]:  # 正文
    DARK[_n] = solve_lightness(GOLD_H, _s, DARK_BG, _need)

# 卡片底单独给（比 gray-100 略亮，卡片要能从页面上「浮」起来）
SURFACE = "#141414"
SURFACE_2 = DARK["gray-100"]

# 语义色：绿色只给成功态。危险是红，警示是琥珀，
# info 原先是 #5bffc1（**是绿的**），按「绿只给成功」必须改掉，
# 改用金色——它不是警示，只是中性提示，与品牌同族不违和。
SEMANTIC = {
    "success-ink": "#34d399",
    "warning-ink": "#fbbf24",
    "danger-ink":  "#f87171",
    "info-ink":    GOLD,
}

# 徽章底：深底上浅色徽章会像一排小灯泡，所以是「暗底 + 亮字」
BADGE = {
    "success-bg": "#10231a",   # 绿调极暗，暗示这是成功色
    "warning-bg": "#2a2410",
    "danger-bg":  "#2e1817",
    "info-bg":    "#241f0e",   # 金调极暗
}

# 金色的**大面积用法**（选中态底、进度条、侧栏高亮）必须更暗，
# 否则一屏金色块会盖过文字。金色只有当中等亮度才既醒目又压得住。
#
# 这里有个**数学上无解的冲突**，必须记下来，否则下一个人会白折腾：
#   · 要金色块在卡片底 #141414 上够显眼      → 金色亮度 ≥ 0.121（3:1）
#   · 要正文色 #cfccc8（亮度 0.606）压上去够清楚 → 金色亮度 ≤ 0.096（4.5:1）
#   交集为空。两个要求不可能同时满足。
#
# 唯一解：选中态文字用**纯白**（亮度 1.0），允许金色亮度落在 0.12~0.18。
# 所以 --on-gold 不是「顺手写个白」，而是这套配色的必要条件——
# 任何选中态（金块 + 文字）都必须用它，否则就是「没高亮」或「看不清」。
# 本轮验算就是在守这条线：正文色压在金块上 3.25:1 是**预期内的不达标**，
# 因为那个组合本来就不该出现。
GOLD_DEEP = hsl_to_hex(GOLD_H, 0.62, 0.335)  # 选中行底 / 侧栏高亮
GOLD_BAR = hsl_to_hex(GOLD_H, 0.70, 0.44)    # 进度条
ON_GOLD = "#ffffff"                          # 压在 GOLD_DEEP 上的文字色


# ---------- 验算 ----------
ok_all = True


def chk(label, fg, bg, need):
    global ok_all
    r = contrast(fg, bg)
    ok = r >= need
    ok_all &= ok
    print("  %-34s %s on %s  %6.2f:1  需 %.1f  %s"
          % (label, fg, bg, r, need, "OK" if ok else "!! 不达标"))
    return ok


print("=" * 74)
print("深色主题 · 中性色阶（底 %s，亮度 %.4f）" % (DARK_BG, luminance(DARK_BG)))
print("=" * 74)
FG_ROLES = [
    ("gray-900", "正文 / --text", 12.0),
    ("gray-800", "强文字", 7.0),
    ("gray-700", "按钮文字 / --bs-btn-color", 7.0),
    ("gray-600", "次要文字 / --text-2", 4.5),   # WCAG AA 硬线
    ("gray-500", "占位 / --text-3", 3.0),
]
for k, role, need in FG_ROLES:
    chk(role, DARK[k], DARK_BG, need)

print()
print("  -- 边框（装饰，无 WCAG 硬线；但低于 1.2 真看不见）--")
for k, need in [("gray-200", 1.25), ("gray-300", 1.60)]:
    chk(k, DARK[k], DARK_BG, need)

print()
print("=" * 74)
print("金色在**每一个底色**上都必须能当文字用")
print("=" * 74)
print("  金色被用在 --primary-text / 链接 / 主按钮文字 / 徽章文字上，")
print("  而这些底色不止一个。逐一验，别只验页面底。")
BACKGROUNDS = [
    ("页面底 --bg", DARK_BG),
    ("卡片 --surface", SURFACE),
    ("次级底 --surface-2", SURFACE_2),
    ("表头底 --head-bg", DARK["gray-100"]),
    ("卡片标题栏 --card-head-solid", "#101010"),
    ("侧栏 --sidebar-bg 上段", "#0d0d0d"),
]
for label, bg in BACKGROUNDS:
    chk("金色作文字 @ " + label, GOLD, bg, 4.5)

print()
print("  -- 金色的反色用法：金色底 + 深字（主按钮）--")
for label, fg in [("按钮文字近黑", "#0a0a0a"), ("按钮文字纯黑", "#000000")]:
    chk("金底 @ " + label, GOLD, fg, 4.5)

print()
print("=" * 74)
print("语义色（在页面底与卡片底上都要过）")
print("=" * 74)
for k, v in SEMANTIC.items():
    chk(k + " @ 页面底", v, DARK_BG, 4.5)
    chk(k + " @ 卡片底", v, SURFACE, 4.5)

print()
print("=" * 74)
print("徽章：暗底 + 亮字")
print("=" * 74)
for bgk, ink in [("success-bg", "success-ink"), ("warning-bg", "warning-ink"),
                 ("danger-bg", "danger-ink"), ("info-bg", "info-ink")]:
    chk("%s 底 + %s 字" % (bgk, ink), SEMANTIC[ink], BADGE[bgk], 4.5)

print()
print("=" * 74)
print("大面积金色（不是文字，只是色块，需 ≥3:1 能被看见）")
print("=" * 74)
chk("选中行底 @ 页面底", GOLD_DEEP, DARK_BG, 3.0)
chk("选中行底 @ 卡片底", GOLD_DEEP, SURFACE, 3.0)
chk("进度条 @ 卡片底", GOLD_BAR, SURFACE, 3.0)
# 进度条里若有文字，那文字得能压在金色上
chk("金色底上的近黑字（进度条内文字）", "#0a0a0a", GOLD_BAR, 4.5)

print()
print("  -- 选中态里的文字压在金色块上：")
print("     上面的无解冲突决定了：只能用纯白。")
chk("选中态白字 @ gold-deep", ON_GOLD, GOLD_DEEP, 4.5)
print()
print("     以下两项**预期不达标**，它们是那两条互斥要求的直接后果，")
print("     不是配色算错了。列出来是为了防止有人以为可以这么用：")
for fg_name, fg in [("正文色 gray-900", DARK["gray-900"]), ("金色字（同色）", GOLD)]:
    r = contrast(fg, GOLD_DEEP)
    print("  %-34s %s on %s  %6.2f:1  （预期不达标：禁止这样用）"
          % ("选中态 " + fg_name, fg, GOLD_DEEP, r))

print()
print("=" * 74)
print("可粘贴的 [data-bs-theme=\"dark\"] 变量块")
print("=" * 74)
print()
print('[data-bs-theme="dark"] {')
print("  /* --- 中性色阶：中性偏暖，暖到 0.06 饱和度上限，再多就显脏 --- */")
for k in sorted(DARK, key=lambda x: int(x.split("-")[1])):
    print("  --%s: %s;" % (k, DARK[k]))
print()
print("  /* --- 表面 --- */")
print("  --surface: %s;" % SURFACE)
print("  --surface-2: var(--gray-100);")
print()
print("  /* --- 品牌色：深底上用金；绿只留给成功态 --- */")
print("  --primary-ink: %s;" % GOLD)
print("  --accent: %s;" % GOLD)
print()
print("  /* --- 语义色文字档 --- */")
for k in SEMANTIC:
    print("  --%s: %s;" % (k, SEMANTIC[k]))
print()
print("  /* --- 徽章底 --- */")
for k in BADGE:
    print("  --%s: %s;" % (k, BADGE[k]))
print()
print("  --gold-deep: %s;" % GOLD_DEEP)
print("  --gold-bar: %s;" % GOLD_BAR)
print("  --on-gold: %s;" % ON_GOLD)
print("}")

print()
print("全部达标" if ok_all else "!! 有不达标项，见上面 !! 行")
sys.exit(0 if ok_all else 1)