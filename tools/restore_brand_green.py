"""把品牌色恢复成**原始值**（不做色相旋转），并给深色主题算一个亮绿。

为什么要单独一步
----------------
reblue_to_green.py 是按「保留明度、色相转绿」批量反转的，
主色 #3e6ca8 也被转成了 #3bab81——同色相、亮度接近，但不是**那个绿**。
品牌色是识别度的一部分，界面左上角的 logo、侧栏高亮、按钮
都应该回到 #3d7a63，所以这些值必须显式写死，不能靠算。

装饰色（中性色阶、渐变端点、莫兰迪）用算的没问题，那些不需要
「认得出是原来那个」。

深色主题的主色不能直接用 #3d7a63：它在深底上只有 2.9:1，会糊掉。
这里按对比度搜一个够亮的同色相绿。
"""
from __future__ import annotations

import colorsys
import io
import re

PATH = "app/static/css/style.css"
HUE = 157.4                       # #3d7a63 的色相
BRAND = "#3d7a63"
LIGHT_INK_MIN = 4.8               # 深色主题主色亮档的对比度下限


def srgb_to_lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum(hx):
    h = hx.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g) + 0.0722 * srgb_to_lin(b)


def contrast(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def comment_mask(text):
    mask = [False] * len(text)
    i, n, c = 0, len(text), False
    while i < n:
        if not c and text.startswith("/*", i):
            c = True
            mask[i] = mask[i + 1] = True
            i += 2
            continue
        if c and text.startswith("*/", i):
            mask[i] = mask[i + 1] = True
            c = False
            i += 2
            continue
        mask[i] = c
        i += 1
    return mask


def block_span(text, start_at, mask):
    depth, i = 0, start_at
    while i < len(text):
        if not mask[i]:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return start_at + 1, i
        i += 1
    raise SystemExit("块没有闭合于 %d" % start_at)


def find_dark_var_block(text, mask):
    """深色**变量**块：含 >=10 个 --gray- 声明，且不含 --surface: #ffffff。
    与 @media print 的回退块区分开。"""
    for m in re.finditer(r'\[data-bs-theme="dark"\]', text):
        q = text.find("{", m.end())
        if q == -1:
            continue
        try:
            o, c = block_span(text, q, mask)
        except SystemExit:
            continue
        body = text[o:c]
        if body.count("--gray-") < 10:
            continue
        if "--surface: #ffffff" in body.replace("  ", " "):
            continue
        return q, c
    raise SystemExit("找不到深色变量块")


# ---- 算深色主题的亮绿 ----
src = io.open(PATH, encoding="utf-8").read()
mask = comment_mask(src)
q, c = find_dark_var_block(src, mask)
dark_bg = re.search(r"--gray-50:\s*(#[0-9a-fA-F]{6})", src[q:c]).group(1)

light_ink = None
for L in [x / 1000 for x in range(300, 900)]:
    r, g, b = colorsys.hls_to_rgb(HUE / 360.0, L, 0.42)
    h = "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
    if contrast(h, dark_bg) >= LIGHT_INK_MIN:
        light_ink = h
        break
if light_ink is None:
    raise SystemExit("解不出对比度 ≥%.1f 的亮绿" % LIGHT_INK_MIN)
print("深色主题底色 %s" % dark_bg)
print("深色主题主色亮档 %s  对底 %.2f:1" % (light_ink, contrast(light_ink, dark_bg)))

# ---- 品牌色恢复（全文，注意 count 限制） ----
REPL = [
    (r"--primary:\s*#[0-9a-fA-F]{6}", "--primary: %s" % BRAND, 1),
    (r"--primary-2:\s*#[0-9a-fA-F]{6}", "--primary-2: #6aa88c", 1),
    (r"--accent:\s*#[0-9a-fA-F]{6}", "--accent: #4e8f73", 1),
    (r"--bs-link-hover-color:\s*#[0-9a-fA-F]{6}", "--bs-link-hover-color: #2c5246", 1),
    (r"--bs-link-color-rgb:\s*[\d,\s]+;", "--bs-link-color-rgb: 61, 122, 99;", 1),
    (r"--bs-link-hover-color-rgb:\s*[\d,\s]+;",
     "--bs-link-hover-color-rgb: 44, 82, 70;", 1),
    (r"--head-fg:\s*#[0-9a-fA-F]{6}", "--head-fg: #2c5246", 1),
    (r"--primary-ink:\s*#[0-9a-fA-F]{6}", "--primary-ink: %s" % BRAND, 1),
    (r"linear-gradient\(135deg,\s*#[0-9a-fA-F]{6}\s*0%,\s*#[0-9a-fA-F]{6}\s*100%\)",
     "linear-gradient(135deg, #3d7a63 0%, #6aa88c 100%)", 1),
    (r"linear-gradient\(90deg,\s*#[0-9a-fA-F]{6},\s*#[0-9a-fA-F]{6}\)",
     "linear-gradient(90deg, #3d7a63, #6aa88c", 2),
]
out = src
n = 0
for pat, rep, cnt in REPL:
    rep = rep + (")" if pat.endswith(r"\)\)") else "")
    out, k = re.subn(pat, rep, out, count=cnt)
    n += k
    print("  %-28s 替换 %d 处" % (pat[:26], k))

# ---- 深色变量块里的四个值 ----
mask2 = comment_mask(out)
q, c = find_dark_var_block(out, mask2)
body = out[q:c]
for key, val in [("--accent", light_ink), ("--primary-ink", light_ink),
                 ("--bs-link-color", light_ink),
                 ("--bs-link-hover-color", "#c2e6d8")]:
    body, k = re.subn(re.escape(key) + r":\s*[^;]+;",
                      key + ": " + val + ";", body, count=1)
    print("  深色块 %-22s -> %s  x%d" % (key, val, k))
out = out[:q] + body + out[c:]

io.open(PATH, "w", encoding="utf-8", newline="").write(out)
print()
print("共改 %d 处" % n)

# ---- 自检 ----
chk = io.open(PATH, encoding="utf-8").read()
mask3 = comment_mask(chk)
q3, c3 = find_dark_var_block(chk, mask3)
dark_body = chk[q3:c3]
tests = [
    ("主色 = #3d7a63", "--primary: #3d7a63" in chk),
    ("primary-2 = #6aa88c", "--primary-2: #6aa88c" in chk),
    ("深色 primary-ink 已换亮绿", ("--primary-ink: %s" % light_ink) in dark_body),
    ("深色链接色已换亮绿", ("--bs-link-color: %s" % light_ink) in dark_body),
]
bad = [n for n, ok in tests if not ok]
for n, ok in tests:
    print("  %-28s %s" % (n, "OK" if ok else "!! 不符"))
print()
print("完成" if not bad else "!! 有不符项")