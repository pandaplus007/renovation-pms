"""深色主题 v3 选型前的约束测算：青色 vs 蓝绿色，以及给定文字色是否达标。

为什么先算再改
--------------
这一版换的是**强调色**（金 -> 青/蓝绿），而成功态那支绿（#22C55E）
还在原位。两支色会不会让用户分不清「已完成」和「这是链接」，
不是审美问题而是**色相距离**问题，可以算出来。

而你给的四个文字/表面色值有两个可能不达标：
    #E5E7EB 主文本、#9CA3AF 次文本
上次踩过的坑就是「按页面底验算了、到了卡片底不够」，
所以这里对**每一个底**都验。

用法：python tools/eval_dark_v3.py
"""
from __future__ import annotations

import colorsys


# ---------- 色彩工具（与其它脚本同源） ----------
def srgb_to_lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hexstr):
    h = hexstr.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g)
            + 0.0722 * srgb_to_lin(b))


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hue_of(hexstr):
    h = hexstr.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hls(r, g, b)[0] * 360


def sat_of(hexstr):
    h = hexstr.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hls(r, g, b)[1]


def hue_dist(a, b):
    """色相环上的最短距离（度）。0 = 同色，180 = 正对面。"""
    d = abs(hue_of(a) - hue_of(b)) % 360
    return min(d, 360 - d)


def hsl_to_hex(h, s, l):
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def solve_lightness(hue, sat, bg, need, dark=True):
    lo, hi = 0.02, 0.98
    best = None
    for _ in range(44):
        mid = (lo + hi) / 2
        ok = contrast(hsl_to_hex(hue, sat, mid), bg) >= need
        if dark:
            if ok:
                best = mid
                hi = mid
            else:
                lo = mid
        else:
            if ok:
                best = mid
                lo = mid
            else:
                hi = mid
    if best is None:
        raise SystemExit("解不出：h=%.0f s=%.2f 对 %s 达 %.2f:1" % (hue, sat, bg, need))
    return hsl_to_hex(hue, sat, min(0.99, best + 0.010))


# ---------- v3 给定值 ----------
BG = "#121212"
SURFACE = "#1E1E1E"
SURFACE_3 = "#2D2D2D"
TEXT_MAIN = "#E5E7EB"     # 主文本
TEXT_SUB = "#9CA3AF"      # 次文本
CYAN = "#06B6D4"
TEAL = "#14B8A6"
ORANGE = "#F97316"       # 高亮辅助 / 警示

# v2 里已在位的语义色（成功态那支绿是这个方案的核心风险点）
SEMANTIC_V2 = {
    "success": "#22C55E",
    "warning": ORANGE,
    "danger":  "#de7a73",
    "info":    "#6c97dd",
}

# ---------- 半透明叠色底：斑马 + 悬停叠在卡片上 ----------
def over(fg, a, bg):
    return [fg[i] * a + bg[i] * (1 - a) for i in range(3)]


def hx(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v))) for v in c)


def parse(c):
    c = c.lstrip("#")
    return [int(c[i:i + 2], 16) for i in (0, 2, 4)]


stacked = over([255, 255, 255], 0.028, parse(SURFACE))
stacked = over([255, 255, 255], 0.035, stacked)
STACKED = hx(stacked)

BASES = [("大背景", BG), ("卡片底", SURFACE), ("叠色底", STACKED), ("弹出层", SURFACE_3)]

print("=" * 78)
print("第 1 步：你给定的四个色值，逐个底验算")
print("=" * 78)
print()
print("  判据：正文 >= 4.5:1（AA）。次文本承载真实信息，不按装饰放宽。")
print()
for label, v, need in [("主文本 #E5E7EB", TEXT_MAIN, 4.5),
                       ("次文本 #9CA3AF", TEXT_SUB, 4.5)]:
    print("  %s" % label)
    for bn, b in BASES:
        r = contrast(v, b)
        print("    @ %-8s %-9s %5.2f:1  需 %.1f  %s"
              % (bn, b, r, need, "OK" if r >= need else "!! 不达标"))
    print()

print("  高亮辅助 #F97316（橙色）")
for bn, b in BASES:
    r = contrast(ORANGE, b)
    print("    @ %-8s %-9s %5.2f:1  需 4.5  %s"
          % (bn, b, r, "OK" if r >= 4.5 else "!! 不达标"))
print()

print("=" * 78)
print("第 2 步：两个强调色候选，逐个底验算")
print("=" * 78)
for name, v in (("青色 #06B6D4", CYAN), ("蓝绿 #14B8A6", TEAL)):
    print()
    print("  %s" % name)
    for bn, b in BASES:
        r = contrast(v, b)
        print("    作文字 @ %-8s %-9s %5.2f:1  需 4.5  %s"
              % (bn, b, r, "OK" if r >= 4.5 else "!! 不达标"))

print()
print("=" * 78)
print("第 3 步：色相冲突 —— 强调色 vs 成功绿")
print("=" * 78)
print()
print("  这是本方案唯一的**结构性风险**：成功态那支绿必须保留")
print("  （「已完成 / 已付清」要一眼可辨），而新强调色是青/蓝绿系。")
print("  两支色靠得太近时，用户分不清「这行是已完成」还是「这行可点」。")
print()
print("  参考量级：")
print("    < 15°   肉眼几乎无法区分，不可用")
print("    15-30°  需要靠亮度或形状辅助区分，勉强")
print("    > 30°   可以分开")
print()
succ = SEMANTIC_V2["success"]
for name, v in (("青色 #06B6D4", CYAN), ("蓝绿 #14B8A6", TEAL)):
    d = hue_dist(v, succ)
    verdict = "!! 太近，不可用" if d < 15 else ("勉强，需辅助区分" if d < 30 else "OK，可以分开")
    print("  %-14s vs 成功绿 %s  色相距离 %5.1f°  饱和度 %.2f/%.2f  %s"
          % (name, succ, d, sat_of(v), sat_of(succ), verdict))
print()
print("  再和「信息蓝」比一次（信息蓝 #6c97dd 也在四色语义里）：")
for name, v in (("青色 #06B6D4", CYAN), ("蓝绿 #14B8A6", TEAL)):
    d = hue_dist(v, SEMANTIC_V2["info"])
    verdict = "!! 太近" if d < 15 else ("勉强" if d < 30 else "OK")
    print("  %-14s vs 信息蓝 %s  色相距离 %5.1f°  %s" % (name, SEMANTIC_V2["info"], d, verdict))

print()
print("=" * 78)
print("第 4 步：若选定的强调色要压「满格底」（主按钮/进度条）")
print("=" * 78)
print()
print("  满格底 + 文字，文字得够。亮青配白字通常不够（青本身很亮），")
print("  所以大概率要配**近黑字**。这里把两个候选都算一遍。")
print()
for name, v in (("青色 #06B6D4", CYAN), ("蓝绿 #14B8A6", TEAL)):
    print("  %s  (亮度 %.4f)" % (name, luminance(v)))
    for fg in ("#ffffff", "#0a0a0a"):
        r = contrast(fg, v)
        print("    %s 作文字压满格%s  %5.2f:1  %s"
              % (fg, name, r, "OK" if r >= 4.5 else "!! 不达标"))
    # 需要压到多深才压得住近黑字
    cap = (luminance(v) + 0.05) / 4.5 - 0.05
    print("    近黑字要 4.5:1 -> 满格底亮度需 >= %.4f（当前 %.4f）%s"
          % (cap, luminance(v), "，已够" if luminance(v) >= cap else "，**当前不够**"))
    print()

print("=" * 78)
print("第 5 步：选中态（金块的等价物）—— 强调色块 + 文字")
print("=" * 78)
print()
for name, accent in (("青色 #06B6D4", CYAN), ("蓝绿 #14B8A6", TEAL)):
    hue = hue_of(accent)
    deep = solve_lightness(hue, 0.55, SURFACE, 3.0)
    print("  %s  选中态底（对卡片底 >=3:1）= %s" % (name, deep))
    for fg in ("#ffffff", TEXT_MAIN, accent):
        r = contrast(fg, deep)
        tag = "（同色，不可用）" if fg == accent else ""
        print("    %-9s 压上去 %5.2f:1  需 4.5  %s%s"
              % (fg, r, "OK" if r >= 4.5 else "!! 不达标", tag))
    print()