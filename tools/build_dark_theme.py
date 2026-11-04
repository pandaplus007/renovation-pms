"""深色主题配色推导器 v3：炭灰三层 + 蓝绿强调 + 四色语义。

v3 相对 v2 改了什么
-------------------
只换强调色与文字两套，表面三层（#121212 / #1E1E1E / #2D2D2D）不变。

  v2 强调色：古铜金 #c9a227
  v3 强调色：蓝绿     #14B8A6   ← 用户在「青色 #06B6D4 / 蓝绿 #14B8A6」里选的

  为什么选蓝绿而不是青色 —— 这是算出来���不是审美偏好：
  成功态那支绿 #22C55E 必须保留（「已完成/已付清」要一眼可辨），
  而青色与信息蓝 #6c97dd 的色相只隔 28.4°，两支语义色靠得太近；
  蓝绿与成功绿隔 31.3°、与信息蓝隔 43.8°，四支色相最散开。
  色相距离的计算见 tools/eval_dark_v3.py。

  v2 主文本：#F1F5F9 / 正文 #CBD5E1
  v3 主文本：#E5E7EB / 次文本 #9CA3AF  ← 用户给定的值，两个都达标

给定色值的处理原则
------------------
用户给的色值**先验算再决定用不用**，达标就照抄，不达标就反解并**说明偏差**。
本轮 #E5E7EB / #9CA3AF / #F97316 / #14B8A6 全部达标，直接采用；
#9CA3AF 用作 --gray-600（次文本）时，比 --gray-800（正文）低一档，
正好对应「正文 / 次文本」两级。

为什么明度仍然要反解
--------------------
用户给定的是「色相 + 某些档位」，中间那些档（边框、悬停、选中态底、
标签底）没人给。而 WCAG 亮度权重 R 0.2126 / G 0.7152 / B 0.0722，
绿光权重最高、蓝色最低 —— **同样的 HSL 明度，不同色相看起来亮度差很多**。
所以低档位必须反解，不能拿 v2 的明度参数直接搬。

用法：python tools/build_dark_theme.py
输出：可粘贴的 [data-bs-theme="dark"] 变量块 + 全部对比度验算
"""
from __future__ import annotations

import sys


# ---------- 色彩工具（与 build_palette.py 同源，保证两套算出来的数可比） ----------
def srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hexstr: str) -> float:
    h = hexstr.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g)
            + 0.0722 * srgb_to_lin(b))


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hsl_to_hex(h: float, s: float, l: float) -> str:
    import colorsys
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, l, s)
    to = lambda v: max(0, min(255, round(v * 255)))
    return "#%02x%02x%02x" % (to(r), to(g), to(b))


def hue_of(hexstr: str) -> float:
    import colorsys
    h = hexstr.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hls(r, g, b)[0] * 360


def hue_dist(a: str, b: str) -> float:
    """色相环上的最短距离（度）。0 = 同色。"""
    d = abs(hue_of(a) - hue_of(b)) % 360
    return min(d, 360 - d)


def solve_lightness(hue: float, sat: float, bg: str, need: float) -> str:
    """二分搜索明度使 对比度(bg) >= need，取**刚好达标**的那一档。

    深色主题：明度越低越暗、对比越低，达标集合是 L >= T，
    收敛方向与浅色主题**相反**——写反会一路跑到 0.98 再报「解不出」，
    而实际 L=0.5 时早就该有 8:1 了。
    """
    lo, hi = 0.02, 0.995
    best = None
    for _ in range(44):
        mid = (lo + hi) / 2
        if contrast(hsl_to_hex(hue, sat, mid), bg) >= need:
            best = mid
            hi = mid
        else:
            lo = mid
    if best is None:
        raise SystemExit("解不出：色相 %.0f° 饱和度 %.2f 对 %s 达到 %.2f:1"
                         % (hue, sat, bg, need))
    return hsl_to_hex(hue, sat, min(0.99, best + 0.010))


def solve_floor_luminance(hue: float, sat: float, fg: str, need: float) -> str:
    """二分搜索明度使 对比度(fg) >= need，方向与 solve_lightness **相反**。

    solve_lightness 拿深色**背景**当参照，达标集合是 L >= T（越亮越够）。
    这里拿**前景文字色**当参照：底越亮字越暗才够，达标集合仍是 L >= T，
    但语义不同 —— 它解出的是「一块底要亮到多少才压得住这个字」。

    两者混用会解出反方向的色：拿 #0a0a0a 去调一个要求压过白字的底时，
    收敛方向相同但需求相反，脚本不报错、只是把色算反。
    """
    lo, hi = 0.02, 0.995
    best = None
    for _ in range(44):
        mid = (lo + hi) / 2
        if contrast(hsl_to_hex(hue, sat, mid), fg) >= need:
            best = mid
            hi = mid
        else:
            lo = mid
    if best is None:
        raise SystemExit("解不出：色相 %.0f° 饱和度 %.2f 压住 %s 达到 %.2f:1"
                         % (hue, sat, fg, need))
    return hsl_to_hex(hue, sat, min(0.99, best + 0.006))


# ---------- 用户给定的色值（先验算，达标就照抄） ----------
BG        = "#121212"   # 大背景（与 v2 相同）
SURFACE   = "#1E1E1E"   # 卡片（与 v2 相同）
SURFACE_3 = "#2D2D2D"   # 悬停态 / 下拉（与 v2 相同）
ACCENT    = "#14B8A6"   # 蓝绿强调色（用户在青色/蓝绿之间选了蓝绿）

TEXT_MAIN = "#E5E7EB"   # 主文本 / 核心数据
TEXT_SUB  = "#9CA3AF"   # 次文本 / 次要信息
TEXT_DIM  = "#64748B"   # 占位符候选（3.94:1，可能要被替）
NAV_IDLE  = "#9CA3AF"   # 侧栏未选中项（与次文本同档，少一层干扰）
ORANGE    = "#F97316"   # 高亮辅助：极需警告 / 核心 CTA

SEMANTIC = {
    "success": "#22C55E",   # 必须保留：已完成/已付清要一眼可辨
    "warning": ORANGE,
    "danger":  "#EF4444",
    "info":    "#6c97dd",   # v2 反解过的值，保留
}

# 强调色的色相，用于反解它的低档位（悬停底 / 选中态底 / 渐变另一端）
ACCENT_H = hue_of(ACCENT)

# 中性色阶的色相：偏**冷中性**（220°），与蓝绿强调色同族。
# 为什么不用暖中性：v2 用暖（45°）是为了配金色，
# v3 强调色变冷之后，暖中性底 + 冷强调会显得脏。
COOL_H = 220.0

ok_all = True
deviations = []


def chk(label, fg, bg, need, note=""):
    global ok_all
    r = contrast(fg, bg)
    ok = r >= need
    ok_all &= ok
    print("  %-40s %s on %s  %6.2f:1  需 %.1f  %s%s"
          % (label, fg, bg, r, need, "OK" if ok else "!! 不达标",
             ("  " + note) if note else ""))
    return ok


print("=" * 78)
print("第 1 步：验算用户直接给定的色值（不反解，只看达不达标）")
print("=" * 78)

chk("主文本 / 核心数据", TEXT_MAIN, BG, 12.0)
chk("主文本 @ 卡片底", TEXT_MAIN, SURFACE, 12.0)
chk("次文本 / 次要信息", TEXT_SUB, BG, 4.5)
chk("次文本 @ 卡片底", TEXT_SUB, SURFACE, 4.5)
chk("侧栏未选中项", NAV_IDLE, SURFACE, 4.5)
print()
print("  -- 高亮辅助（橙色）：用户原话「仅用于极需警告或核心 CTA」--")
chk("橙色作文字 @ 大背景", ORANGE, BG, 4.5)
chk("橙色作文字 @ 卡片底", ORANGE, SURFACE, 4.5)
chk("橙色作文字 @ 叠色底", ORANGE, hx(stacked) if "stacked" in dir() else "#2c2c2c", 4.5)
print()
print("  -- 占位符候选 %s --" % TEXT_DIM)
_r = contrast(TEXT_DIM, BG)
print("     实测 %.2f:1" % _r)
if _r < 3.5:
    print("     → 占位符只用 3.5:1 的地板（WCAG 不要求它达标，")
    print("       它是提示不是数据），但 3.94 够，下面直接采用。")

print()
print("=" * 78)
print("第 2 步：中性色阶（文字档按对比度反解，表面档按给定值）")
print("=" * 78)

DARK = {
    "gray-50":  BG,          # 大背景，用户给定
    "gray-100": "#181818",   # 次级底 / 空状态，用户给定
    "gray-200": solve_lightness(COOL_H, 0.0, BG, 1.30),    # 边框
    "gray-300": solve_lightness(COOL_H, 0.0, BG, 1.75),    # 强边框 / 输入框描边
    "gray-400": solve_lightness(COOL_H, 0.02, BG, 2.70),   # 禁用态 / 装饰
    "gray-500": solve_lightness(COOL_H, 0.03, BG, 3.90),   # 占位符
    # ⚠ 目标值必须**卡在卡片底上验，不是页面底上验**。
    #   这一档会被用在卡片、表单、侧栏多种底色上，页面底比卡片底暗，
    #   同一支色在卡片上对比度更低。v2 按页面底解出 4.75:1，
    #   实际在卡片底只有 4.23:1 —— 234 处不达标全来自这一档。
    "gray-600": TEXT_SUB,                                    # 次文本，用户给定 5.50:1
    "gray-700": solve_lightness(COOL_H, 0.04, BG, 7.50),    # 导航/侧栏项（需亮于次文本）
    "gray-800": solve_lightness(COOL_H, 0.04, SURFACE, 11.0),  # 正文
    "gray-900": TEXT_MAIN,                                  # 主文本，用户给定 13.47:1
}

print()
# ⚠ 每档要同时对**两个底**验。只对页面底验是不够的：
#   --gray-600 会用在卡片里，卡片底更亮所以对比度更低。
for k in sorted(DARK, key=lambda x: int(x.split("-")[1])):
    given = {"gray-50": BG, "gray-900": TEXT_MAIN, "gray-600": TEXT_SUB,
             "gray-100": "#181818"}.get(k)
    r_bg = contrast(DARK[k], BG)
    r_su = contrast(DARK[k], SURFACE)
    flag = ""
    # 哪些档承载「必须读到的文字」？
    #   600/700/800/900：是（次要信息、导航、正文、主文本）
    #   400/500：**不是** —— 400 是禁用态与装饰，500 是占位符。
    #     占位符按 WCAG 不属于必须达 4.5 的内容（它是提示，不是数据），
    #     但也不能低到看不见，所以给 3.5 的地板。
    #     一开始我把 400/500 也按 4.5 判，脚本报了 2 个假失败，
    #     差一步就去把禁用态文字调亮——那会破坏「禁用」这个语义本身。
    if k in ("gray-600", "gray-700", "gray-800", "gray-900"):
        need = 4.5
    elif k == "gray-500":
        need = 3.5
    else:
        need = None
    if need is not None and r_su < need:
        flag = "   !! 卡片底只有 %.2f:1 < %.1f" % (r_su, need)
        ok_all = False
    print("  --%-9s %-9s  大背景 %5.2f:1   卡片底 %5.2f:1%s%s"
          % (k, DARK[k], r_bg, r_su,
             "   （用户给定）" if given else "", flag))

print()
print("  -- 表面三层（用户给定的抬升关系）--")
chk("卡片底 比大背景亮", SURFACE, BG, 1.0)
chk("悬停/下拉底 比卡片亮", SURFACE_3, SURFACE, 1.0)
print("     #121212 -> #1E1E1E -> #2D2D2D  三层抬升，"
      "卡片靠**明度差 + 1px 描边**双保险分层（只靠色差在 OLED 上会糊）。")

print()
print("=" * 78)
print("第 3 步：蓝绿强调色 —— 每一个用它当底或当字的地方都要验")
print("=" * 78)
print("  强调色承担四件事（沿用 v2 与你确认过的收敛范围）：")
print("    1. 表格里的链接（项目名/单号）")
print("    2. 主按钮")
print("    3. 进度条")
print("    4. 金额强调")
print("  侧栏图标、未选中导航、状态徽章、正文**一律不用强调色**。")
print()
print("  色相冲突检查（四支语义色两两之间的距离）：")
print("  < 15° 肉眼无法区分；15-30° 需靠亮度辅助；> 30° 可分开")
for other in ("success", "warning", "danger", "info"):
    d = hue_dist(ACCENT, SEMANTIC[other])
    v = "!! 太近" if d < 15 else ("勉强" if d < 30 else "OK")
    print("    vs %-8s %-8s %5.1f°  %s" % (other, SEMANTIC[other], d, v))

print()
print("  -- 强调色作文字（链接）--")
# 链接在最亮的叠色底上也要够 —— 表格里的项目名链接就在那一档底上
ACC_TEXT = solve_lightness(ACCENT_H, 0.40, "#2c2c2c", 4.6)
for label, bg in [("大背景", BG), ("卡片底", SURFACE),
                  ("叠色底（表格行）", "#2c2c2c"), ("弹出层", SURFACE_3)]:
    chk("链接色 @ " + label, ACC_TEXT, bg, 4.5)
print("     原定 %s 作文字在大背景 %5.2f:1，够；下面用的是同色相"
      % (ACCENT, contrast(ACCENT, BG)))
print("     但为叠色底抬亮后的 %s（两者色相相同）" % ACC_TEXT)

print()
print("  -- 满格底 + 近黑字（主按钮、tab 选中）--")
ACC_FULL = solve_lightness(ACCENT_H, 0.40, SURFACE, 7.5)
chk("主按钮底 @ 卡片底（够显眼）", ACC_FULL, SURFACE, 3.0)
chk("主按钮上的近黑字", "#0a0a0a", ACC_FULL, 4.5)
# 对照行：亮青绿上用白字必然不达标，故意展示，不计入 ok_all
print("  %-40s %s on %s  %6.2f:1  ← 对照：亮青绿上用白字就是这个数，不可用"
      % ("满格强调底 + 白字（禁止）", "#ffffff", ACC_FULL, contrast("#ffffff", ACC_FULL)))

print()
print("  -- 选中态 / 进度条 --")
ACC_DEEP = solve_lightness(ACCENT_H, 0.50, SURFACE, 3.0)
ACC_BAR = solve_lightness(ACCENT_H, 0.55, SURFACE, 4.0)
chk("选中态底 @ 卡片底", ACC_DEEP, SURFACE, 3.0)
chk("进度条 @ 卡片底", ACC_BAR, SURFACE, 3.0)
chk("选中态白字", "#ffffff", ACC_DEEP, 4.5)
chk("进度条上的近黑字", "#0a0a0a", ACC_BAR, 4.5)
for nm, fg in [("主文本色", TEXT_MAIN), ("强调色（同色）", ACCENT)]:
    r = contrast(fg, ACC_DEEP)
    print("  %-40s %s on %s  %6.2f:1  （**预期不达标：禁止这样用**）"
          % ("选中态 " + nm, fg, ACC_DEEP, r))

print()
print("  -- 主按钮渐变的**深端**必须另算，不能沿用 --accent-deep --")
# 这条是本轮踩到的一个真问题，值得留在这里：
#   白字要 4.5:1  -> 底亮度上限 0.115
#   近黑字要 4.5:1 -> 底亮度下限 0.1940
# 两者**无交集**，所以「同一条渐变上一端配白字、另一端配近黑字」不可能成立。
# 金色那一版能成立是因为整条渐变都够暗（亮度 0.119~0.155），
# 只够压白字；蓝绿这一版整条都亮（0.20~0.37），只能压近黑字。
# 换句话说：**一条渐变只能用一种文字色**，深端要跟着走另一种路线重新反解。
_hi = 4.5 * (luminance("#ffffff") + 0.05) - 0.05
_lo = 4.5 * (luminance("#0a0a0a") + 0.05) - 0.05
print("     白字 4.5:1 要求底亮度 <= %.4f；近黑字要求 >= %.4f" % (_hi, _lo))
print("     两者%s（所以渐变必须统一一种文字色）"
      % ("**无交集**" if _hi < _lo else "有交集，两种字都能用"))
print("     本轮统一用近黑字（蓝绿本身就亮），深端按「近黑 4.5:1」反解。")
ACC_GRAD_LO = solve_floor_luminance(ACCENT_H, 0.40, "#0a0a0a", 4.5)
chk("渐变深端上的近黑字", "#0a0a0a", ACC_GRAD_LO, 4.5)
chk("渐变浅端（= 满格底）上的近黑字", "#0a0a0a", ACC_FULL, 4.5)
print("     深端 %s（亮度 %.4f）；沿用白字路线的 %s 只有 %.2f:1，不可用"
      % (ACC_GRAD_LO, luminance(ACC_GRAD_LO), ACC_DEEP,
         contrast("#0a0a0a", ACC_DEEP)))

print()
print("=" * 78)
print()
print("=" * 78)
print("第 5 步：统计卡顶部那条色带 —— 已不需要推导")
print("=" * 78)
print("  v2 有意把这一组**收敛成单色**（--stat-accent = --primary），")
print("  理由是「一屏十六张统计卡排开就是一片彩色顶条」，")
print("  而这些色带只是装饰（区分「这张卡是哪类指标」），不承担状态语义。")
print("  浅色侧同样收敛，两边一致。")
print()
print("  ⚠ v3 我在这里写过一套七条分类色并配了整套验算，")
print("    结果 CSS 里那条规则组写在**下面**，把七条全盖掉了 ——")
print("    六张卡渲出来一个颜色，整套推导连同「色相要与成功绿散开」")
print("    的结论全是**死代码**。已删。")
print("    与本轮那条 --primary-grad 重复声明是同一个病根：")
print("    **写在后面的规则赢**，而加新样式时人只会往下加。")
print()
print("  只剩 is-pos / is-neg 两条，它们不是装饰，直接走语义色")
print("  （--success-ink / --danger-ink）。它们在下面第 4 步验算。")

print("=" * 78)
print("第 5 步：半透明层叠后的实际底色（最容易漏的一类）")
print("=" * 78)
# 表格行同时可能有：斑马纹(白 2.8%) + 悬停(白 3.5%) 两层半透明，
# 叠在卡片底上 = 一个比 SURFACE 更亮、且**不带任何色相**的底。
# 语义色（尤其红）压在这上面会比压纯卡片底更低。
# 这一类问题在浏览器里表现为「某个金额是 4.17:1，很难查」——
# 因为单看每一层都合规，只有真去渲染才看得见叠加结果。
def over(fg, alpha, bg):
    return [fg[i] * alpha + bg[i] * (1 - alpha) for i in range(3)]


def hx(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v))) for v in c)


def parse(c):
    c = c.lstrip("#")
    return [int(c[i:i + 2], 16) for i in (0, 2, 4)]


card = parse(SURFACE)
stacked = over([255, 255, 255], 0.028, card)
stacked = over([255, 255, 255], 0.035, stacked)
print("     斑马 2.8%% + 悬停 3.5%% 叠在卡片底上 = %s" % hx(stacked))
print("     （下面这几行只是**预告**：此刻还没反解，用的是用户原定值，")
print("      所以红/蓝显示不达标是正常的。反解在下一步，结果见那里。）")
for k, ink in SEMANTIC.items():
    r = contrast(ink, hx(stacked))
    print("     %-8s %s on %s  %5.2f:1  需 4.5  %s"
          % (k, ink, hx(stacked), r, "OK" if r >= 4.5 else "→ 下一步反解"))

print()
print("=" * 78)
print("第 4 步：四色语义 —— 每个底都要验（大背景与卡片差 0.5 个档）")
print("=" * 78)
print()
print("  ⚠ 语义色与强调色的分界：强调色负责「这是链接/这是金额」，")
print("    语义色负责「这是状态」。两者色相必须隔开，")
print("    否则用户分不清「这行可点」和「这行已完成」。")
print("    success 那支绿**必须保留**：它是「已完成/已付清」，")
print("    是这套系统里唯一不能靠形状或文字区分的状态。")
SEM = {}
SEM_HUE = {"success": 150, "warning": 30, "danger": 4, "info": 217}
for k, v in SEMANTIC.items():
    for bgname, bg in (("大背景", BG), ("卡片底", SURFACE)):
        r = contrast(v, bg)
        need = 4.5
        good = r >= need
        # ⚠ 这里**先不写 ok_all**：不达标时会立刻换成反解值，
        #   若先把失败记进 ok_all，换完值那个标记也不会撤销，
        #   脚本就永远退出 1，读的人会以为还有问题——
        #   「换了值但没撤销失败标记」是我写这个脚本时踩的坑。
        print("  %-8s %-8s @ %-8s %5.2f:1  需 %.1f  %s"
              % (k, v, bg, r, need, "OK" if good else "!! 不达标，改用反解值"))
    # 反解的基准底必须是**最亮的那个**，不是卡片底。
    #   这一档语义色会用在大背景、卡片、以及「斑马+悬停两层半透明叠在
    #   卡片上」的表格行里。第三种最亮（#2c2c2c），拿它当基准才算数。
    #   第一版按卡片底（#1E1E1E）反解，红 4.43:1 看着差一点，
    #   到叠色底上只剩 3.71:1 —— 而红恰恰是最容易撞上叠色底的那个
    #   （表格里的负数金额、逾期金额全是红）。
    BASE_FOR_SEM = hx(stacked)
    if contrast(v, BASE_FOR_SEM) < 4.6:
        fixed = solve_lightness(SEM_HUE[k], 0.62, BASE_FOR_SEM, 4.6)
        SEM[k] = fixed
        deviations.append((v, contrast(v, BASE_FOR_SEM), 4.5, fixed, BASE_FOR_SEM))
        print("        → 在最亮的使用底 %s 上不达标，改为 %s（%.2f:1）"
              % (BASE_FOR_SEM, fixed, contrast(fixed, BASE_FOR_SEM)))
    else:
        SEM[k] = v
    ok_all &= contrast(SEM[k], BASE_FOR_SEM) >= 4.5

print()
print("  -- 语义色的反色用法：暗底 + 亮字（徽章、计数芯片）--")
# 徽章底**反解**而不是写死：不同语义色亮度差很多，
# 同一套底色会让某一档刚好压线（第一版 info 就 4.28:1 卡在下面）。
# 做法是给一个「底色该有多暗」的亮度上限：暗到能让该语义色达 4.6:1。
BADGE_BG = {}
BADGE_HUE = {"success": 150, "warning": 30, "danger": 4, "info": 217}
for k, ink in SEM.items():
    # 底色亮度上限 = (lum_ink + 0.05)/4.6 - 0.05，再往下取 2% 余量
    cap = (luminance(ink) + 0.05) / 4.6 - 0.05
    lum_ink = luminance(ink)
    lo, hi, best = 0.0, 0.6, None
    for _ in range(40):                      # 求「亮度 ≤ cap 的最亮那一档」
        mid = (lo + hi) / 2
        h = hsl_to_hex(BADGE_HUE[k], 0.30, mid)
        if luminance(h) <= cap:
            best = mid
            lo = mid
        else:
            hi = mid
    bg = hsl_to_hex(BADGE_HUE[k], 0.30, max(0.02, best - 0.02))
    BADGE_BG[k] = bg
    chk("%s 徽章：底 + 字" % k, ink, bg, 4.5)

print()
print("=" * 78)
print("可粘贴的 [data-bs-theme=\"dark\"] 变量块")
print("=" * 78)
print()
print('[data-bs-theme="dark"] {')
print("  /* --- 中性色阶：冷中性，与蓝绿强调色同族 --- */")
for k in sorted(DARK, key=lambda x: int(x.split("-")[1])):
    print("  --%s: %s;" % (k, DARK[k]))
print()
print("  /* --- 表面三层：抬升感靠明度差 + 描边双保险 --- */")
print("  --surface:   %s;" % SURFACE)
print("  --surface-2: var(--gray-100);")
print("  --surface-3: %s;   /* 悬停态 / 下拉菜单 */" % SURFACE_3)
print("  --card-border: rgba(255, 255, 255, .08);")
print()
print("  /* --- 输入框要比背景更暗才有「凹陷、可以往里填」的感觉 --- */")
print("  --input-bg: #0E0E0E;")
print("  --input-border: #404040;")
print()
print("  /* --- 强调色：蓝绿，只做四件事（链接 / 主按钮 / 进度条 / 金额）--- */")
print("  --primary-ink: %s;      /* 链接文字（对叠色底抬亮过）*/" % ACC_TEXT)
print("  --accent: %s;           /* 用户原定值 */" % ACCENT)
print("  --accent-deep: %s;      /* 选中态底（侧栏选中 / 表格选中行）*/" % ACC_DEEP)
print("  --accent-bar: %s;       /* 进度条 / 悬停 */" % ACC_BAR)
print("  --accent-full: %s;      /* 满格底（主按钮 / tab 选中）*/" % ACC_FULL)
print("  --on-accent: #ffffff;         /* 压在 --accent-deep 上 */")
print("  --on-accent-bright: #0a0a0a; /* 压在 --accent-full 与整条渐变上 */")
# 渐变深端用 ACC_GRAD_LO 而不是 ACC_DEEP：见上面「无交集」那段推导。
print("  --primary-grad: linear-gradient(135deg, %s 0%%, %s 100%%);" % (ACC_GRAD_LO, ACC_FULL))
print("  --primary-grad-hover: linear-gradient(135deg, %s 0%%, %s 100%%);" % (ACC_FULL, ACC_GRAD_LO))
print("  --on-active-grad: #0a0a0a;")
print()
print("  /* --- 四色语义：绿=已完成 必须保留 --- */")
for k in ("success", "warning", "danger", "info"):
    print("  --%s-ink: %s;" % (k, SEM[k]))
print()
print("  /* --- 徽章底（暗底 + 亮字）--- */")
for k in ("success", "warning", "danger", "info"):
    print("  --%s-bg: %s;" % (k, BADGE_BG[k]))
print()
print("  /* --- 高亮辅助：橙，仅用于极需警告 / 核心 CTA --- */")
print("  --highlight: %s;" % ORANGE)
print()


print("}")

print()
print("=" * 78)
if deviations:
    print("!! 与用户给定色值的偏差 %d 处（都是为了让对比度达标）：" % len(deviations))
    for d in deviations:
        if len(d) == 3:
            print("   %s（原定）实测 %.2f:1 < %.1f:1 → 已按达标值反解"
                  % (d[0], d[1], d[2]))
        else:
            base = ("，基准底 %s" % d[4]) if len(d) > 4 else ""
            print("   %s（原定）在使用底上 %.2f:1 < %.1f:1%s → 改为 %s"
                  % (d[0], d[1], d[2], base, d[3]))
else:
    print("用户给定的色值全部达标，未作任何偏离。")
print()
print("全部达标" if ok_all else "!! 有不达标项，见上面 !! 行")
sys.exit(0 if ok_all else 1)