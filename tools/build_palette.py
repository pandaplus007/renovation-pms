"""配色推导器：算出蓝调中性色阶 + 深色主题，并逐个验算 WCAG 对比度。

为什么用脚本而不是手挑颜色
------------------------
这套界面每一档中性色都标着「必须满足 XX:1」——正文 4.5:1 是 WCAG AA 的
硬线，注释里那几个 8.1:1 / 5.6:1 是当初验算过的。手填一个「看着差不多」的
蓝，十有八九会让 4.5:1 悄悄破掉，而破了对比度在屏幕上**看不出来**，
只能靠读数发现。所以色值必须算出来，且每一步都验。

两个主题的对比度基准不同，别混用一套数：
  · 浅色主题：文字压在近白底上 → 要求高
  · 深色主题：文字压在近黑底上 → 但**底色抬得越高越难达标**，
    深灰底上的中灰文字是深色主题最常见的翻车点

用法：python tools/build_palette.py
输出：可直接粘进 style.css 的 :root 与 [data-bs-theme=dark]
"""
from __future__ import annotations


# ---------- 色彩工具 ----------
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


# ---------- 色相 ----------
# 主色 #3E6CA8 的实际色相
def hue_of(hexstr: str) -> float:
    h = hexstr.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        return 0.0
    if mx == r:
        hp = ((g - b) / d) % 6
    elif mx == g:
        hp = (b - r) / d + 2
    else:
        hp = (r - g) / d + 4
    return hp * 60


import sys

# 主色可传参。默认用原来的低饱和清新绿 #3d7a63。
# 曾经短暂换成过蓝色 #3E6CA8，结论是「不如绿色」：蓝色在中文界面里偏冷硬，
# 且莫兰迪强调色（燕麦/藕粉/陶土/豆沙绿）本来就是围绕绿调一起设计的，
# 换蓝之后那组色失去了依托。所以改回绿。
# 但**深色主题保留**——深浅两个主题与色相无关，且深色下对比度必须重推，
# 不能靠「把颜色反过来」了事。
PRIMARY = sys.argv[1] if len(sys.argv) > 1 else "#3d7a63"
H = hue_of(PRIMARY)
print("主色 %s  色相 %.1f°" % (PRIMARY, H))

# 现有绿色系统的实测值——作为「不能比现在更差」的基准线
_OLD_BG = "#f6f9f7"
REF_L_BORDER = contrast("#e2eae5", _OLD_BG)
REF_L_BORDER_STRONG = contrast("#d3ded8", _OLD_BG)
REF_L_MUTED = contrast("#5a6b64", _OLD_BG)
print("现值基准（绿系，勿降低）：边框 %.2f / 强边框 %.2f / 次要文字 %.2f"
      % (REF_L_BORDER, REF_L_BORDER_STRONG, REF_L_MUTED))


def build_ramp(hue: float, base_s: float, steps: dict) -> dict:
    """按 {名字: (饱和度, 亮度)} 生成色阶。饱和度随亮度反向走：
    深色和浅色两端都降一点，中间最饱和——这是人眼看色阶的规律，
    等饱和度会让两端显得脏。"""
    return {name: hsl_to_hex(hue, s, l) for name, (s, l) in steps.items()}


# ---------- 浅色主题中性色阶 ----------
# 目标（对 --gray-50 近白底）：
#   900  正文           >= 12:1  (AAA)
#   700  强文字/表头    >= 7:1
#   600  次要文字 text-muted >= 4.5:1  ← WCAG AA 硬线
#   500  占位/装饰      ~3.5:1  不承载信息
#   400  边框/禁用态    ~2.2:1
# ---------- 按对比度目标反解明度 ----------
# 为什么不直接写死一串明度：绿光在 WCAG 公式里的亮度权重是 0.7152（最高），
# 蓝色是 0.0722（最低）。**同样的 HSL 明度，绿色看起来比蓝色亮得多**。
# 所以拿蓝色那套明度参数去生成绿色相，会同时击穿 gray-700/600/500 三档
# （实测 6.41 / 4.33 / 2.82，全部不达标）。手工往回调是试错——
# 换个主色又得重来一遍，而且没人说得清每个值为什么是这个数。
#
# 改成反解：给定「这一档要达到的对比度」，二分搜索出满足条件的明度，
# 再压 1% 余量。好处是换主色只改命令行参数，整条色阶自动重新达标。
def solve_lightness(hue: float, sat: float, bg: str, need: float,
                    dark: bool = False) -> tuple:
    """二分搜索明度，使对比度 ≥ need，并取**刚好达标**的那一档。

    关键：明度与对比度的关系是单调的，但**方向随主题相反**——
      浅色主题：明度越低 → 颜色越暗 → 对比越高。达标集合是 L ≤ T。
      深色主题：明度越低 → 越暗 → 对比越低。达标集合是 L ≥ T。
    所以两次搜索的收敛方向是相反的，写反了会一直往错的方向跑，
    最后报「解不出」——而实际值明明存在（L=0.30 时对比度就有 7.07:1）。

    取「刚好达标」而不是「最深/最亮」，是为了让整条色阶保持均匀：
    每一档都贴着自己的对比度下限走，不会出现某几档特别深。
    """
    lo, hi = 0.02, 0.98
    best = None
    for _ in range(40):
        mid = (lo + hi) / 2
        ok = contrast(hsl_to_hex(hue, sat, mid), bg) >= need
        if dark:
            # 达标集合 L ≥ T：达标则往更小试，不达标则往更大试
            if ok:
                best = mid
                hi = mid
            else:
                lo = mid
        else:
            # 达标集合 L ≤ T：达标则往更大试（更浅），不达标则往更小试
            if ok:
                best = mid
                lo = mid
            else:
                hi = mid
    if best is None:
        raise SystemExit(
            "色相 %.0f° 饱和度 %.2f 下解不出对比度 ≥%.1f 的颜色（对 %s）"
            % (hue, sat, need, bg))
    # 留 1% 余量，别正好卡在线上（不同实现的舍入略有差异）
    ml = best + 0.012 if dark else best - 0.012
    return ml, hsl_to_hex(hue, sat, ml)


LIGHT_BG_SELF = hsl_to_hex(H, 0.30, 0.975)     # 页面底：它不承载文字，明度固定
LIGHT = {"gray-50": LIGHT_BG_SELF}
for _n, _s, _need in [
        ("gray-100", 0.26, 1.05),   # 次级底：只需比页面底略深
        ("gray-200", 0.22, 1.15),   # 表格分隔线（装饰）
        ("gray-300", 0.18, 1.40),   # 输入框描边：要比现值更清晰
        ("gray-400", 0.16, 2.20),
        ("gray-500", 0.17, 3.00),   # 占位/装饰，不承载信息
        ("gray-600", 0.20, 4.60),   # 次要文字 text-muted：WCAG AA 硬线
        ("gray-700", 0.21, 7.00),   # 表头 / 强文字
        ("gray-800", 0.22, 10.0),
        ("gray-900", 0.24, 12.0)]:  # 正文
    _l, LIGHT[_n] = solve_lightness(H, _s, LIGHT_BG_SELF, _need)

# ---------- 深色主题中性色阶 ----------
# 深色主题的底**不是纯黑**（纯黑 + 纯白对比过冲、发涩），
# 而是抬到 L≈10% 的深色，白字压上去 15:1 上下，不刺眼。
DARK_BG_SELF = hsl_to_hex(H, 0.24, 0.100)
DARK = {"gray-50": DARK_BG_SELF}
for _n, _s, _need in [
        ("gray-100", 0.22, 1.10),   # 卡片
        ("gray-200", 0.20, 1.20),   # 边框
        ("gray-300", 0.18, 1.60),   # 强边框
        ("gray-400", 0.16, 2.60),
        ("gray-500", 0.15, 3.40),
        ("gray-600", 0.16, 5.00),
        ("gray-700", 0.18, 7.50),   # 次要文字 text-muted
        ("gray-800", 0.20, 10.0),
        ("gray-900", 0.22, 12.0)]:  # 正文
    _l, DARK[_n] = solve_lightness(H, _s, DARK_BG_SELF, _need, dark=True)


def report(name: str, ramp: dict, bg: str, fg_roles: dict) -> bool:
    print()
    print("=== %s（文字压在 %s 上）===" % (name, bg))
    ok_all = True
    for key, (role, need) in fg_roles.items():
        c = ramp[key]
        ratio = contrast(c, bg)
        ok = ratio >= need
        ok_all &= ok
        print("  %-10s %-22s %s  %5.2f:1  需 %.1f  %s"
              % (key, role, c, ratio, need, "OK" if ok else "!! 不达标"))
    # 边框的阈值**不能拍脑袋定**，先看现有系统实测值，别把标准定得比现状还严。
    # WCAG 1.4.11 要求 3:1 的是「传达状态的 UI 组件」；表格分隔线、卡片描边
    # 属于纯装饰，没有硬性要求——低于 1.2 才真看不见。现值是 1.15/1.38，
    # 所以新色阶不低于这两个数即可。
    for key, label, need, ref in [("gray-200", "常规边框（装饰）", 1.15, REF_L_BORDER),
                                  ("gray-300", "强边框/输入框描边", 1.38, REF_L_BORDER_STRONG)]:
        ratio = contrast(ramp[key], bg)
        ok = ratio >= need
        ok_all &= ok
        print("  %-10s %-22s %s  %5.2f:1  需 %.1f（现值 %.2f）  %s"
              % (key, label, ramp[key], ratio, need, ref, "OK" if ok else "!! 不达标"))
    return ok_all


LIGHT_BG = LIGHT["gray-50"]
DARK_BG = DARK["gray-50"]
WHITE = "#ffffff"
PRIMARY_DARK_BG = "#111a24"     # 深色主题的「卡片」底，算按钮文字用

print()
print("=" * 70)
print("主色自身")
print("=" * 70)
print("  主色 %s  对白底文字 %.2f:1（按钮用，需 >=4.5）%s"
      % (PRIMARY, contrast(PRIMARY, WHITE),
         "OK" if contrast(PRIMARY, WHITE) >= 4.5 else "!!"))
# 深色主题上按钮是「浅底深字」还是「深底浅字」取决于实现，
# 两种都要能配出可读的文字色
p_light = hsl_to_hex(H, 0.42, 0.78)
print("  主色亮档 %s  对深底 %.2f:1（深色主题按钮文字，需 >=4.5）%s"
      % (p_light, contrast(p_light, DARK_BG),
         "OK" if contrast(p_light, DARK_BG) >= 4.5 else "!!"))
print("  主色作文字：对浅底 %.2f:1" % contrast(PRIMARY, LIGHT_BG))

l_ok = report("浅色主题 · 中性色阶", LIGHT, LIGHT_BG, {
    "gray-900": ("正文", 12.0),
    "gray-700": ("表头/强文字", 7.0),
    "gray-600": ("次要文字 text-muted", 4.5),
    "gray-500": ("占位/装饰（不承载信息）", 3.0),
})
d_ok = report("深色主题 · 中性色阶", DARK, DARK_BG, {
    "gray-900": ("正文", 12.0),
    "gray-800": ("强文字", 7.0),
    "gray-700": ("次要文字 text-muted", 4.5),
    "gray-600": ("弱化文字", 3.0),
})

# ---------- 深色主题下语义色要重新算 ----------
# 底色变深之后，原来的 --success/#10b981 等直接拿来当底色配白字仍然 OK，
# 但**当文字色**必须在深底上重新验，否则暗底 + 暗字 = 看不见。
print()
print("=" * 70)
print("深色主题语义色「文字档」（在深底上重新验算）")
print("=" * 70)
SEMANTIC = {
    "success": "#34d399",
    "warning": "#fbbf24",
    "danger":  "#f87171",
    "info":    PRIMARY_DARK_BG and "#60a5fa",
}
for k, v in SEMANTIC.items():
    r = contrast(v, DARK_BG)
    print("  %-8s %s  对深底 %5.2f:1  %s"
          % (k, v, r, "OK" if r >= 4.5 else "!! 不达标"))

print()
print("=" * 70)
print("浅色主题语义色文字档（沿用现值，重新对浅底验算）")
print("=" * 70)
SEM_LIGHT = {"success-ink": "#1d6b52", "warning-ink": "#8a5a06",
             "danger-ink": "#a83a30", "info-ink": "#0f5f7a"}
for k, v in SEM_LIGHT.items():
    r = contrast(v, LIGHT_BG)
    print("  %-12s %s  对浅底 %5.2f:1  %s"
          % (k, v, r, "OK" if r >= 4.5 else "!! 不达标"))

print()
print("=" * 70)
print("可粘贴的 CSS 变量")
print("=" * 70)
print()
print(":root {")
for k in sorted(LIGHT, key=lambda x: int(x.split("-")[1])):
    print("  --%s: %s;" % (k, LIGHT[k]))
print("}")

print()
print('[data-bs-theme="dark"] {')
for k in sorted(DARK, key=lambda x: int(x.split("-")[1])):
    print("  --%s: %s;" % (k, DARK[k]))
print("}")

print()
print("全部达标" if (l_ok and d_ok) else "!! 有不达标项，见上面 !! 行")