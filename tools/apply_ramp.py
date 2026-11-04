"""把中性色阶按**块**写入，而不是全文件替换。

上一版栽在这里
--------------
我先跑一遍「把全文所有 --gray-k: 换成浅色值」，再跑一遍「把全文所有
--gray-k: 换成深色值」。结果 `:root` 的色阶被第二遍覆盖成了深色的——
页面底变成 #13201b，浅色主题直接变成黑底，而 CSS 结构检查完全查不出来
（语法没问题、变量都定义了、只是值错了）。

教训：同名变量在**多个块**里各有一份时，不能用全文件替换。
必须先定位到具体的块，再只改那个块。

三处需要写入：
  1. `:root { ... }`                      → 浅色色阶
  2. `[data-bs-theme="dark"] { ... }`     → 深色色阶（变量块，不是下面那堆组件补丁）
  3. `@media print { [data-bs-theme="dark"] { ... } }` → 回退到浅色，
     因为打印永远是纸上白底黑字
"""
from __future__ import annotations

import io
import re
import sys

PATH = "app/static/css/style.css"

LIGHT = {"50": "#f7fbf9", "100": "#ecf4f1", "200": "#deeae6", "300": "#c5d7d0",
         "400": "#90afa3", "500": "#6e9888", "600": "#4f7768", "700": "#3a594d",
         "800": "#294138", "900": "#20342d"}
DARK = {"50": "#13201b", "100": "#1c2c26", "200": "#22332d", "300": "#31473f",
        "400": "#4b685d", "500": "#5a7a6e", "600": "#6f9788", "700": "#97b7ab",
        "800": "#b8d0c7", "900": "#d0e1da"}

src = io.open(PATH, encoding="utf-8").read()


def comment_mask(text: str) -> list:
    """标出每个字符是否位于注释内。

    花括号计数**必须**先屏蔽注释，否则会在注释里那句
    「.small { font-size: .875em }」上多算一对，直接把块的边界数错，
    报出来的却是「块没有闭合」——离真实原因（注释里有花括号）十万八千里。
    """
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


def block_span(text: str, start_at: int, mask: list) -> tuple:
    """返回 (块内容起, 块内容止) 的下标。start_at 指向 '{'。"""
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
    raise SystemExit("块没有闭合，起始位置 %d（行 %d）"
                     % (start_at, text.count("\n", 0, start_at) + 1))


def fill(text: str, open_idx: int, close_idx: int, ramp: dict) -> tuple:
    """把块内所有 --gray-k: 改成 ramp[k] 的值。返回 (新文本, 改了幾处)。"""
    body = text[open_idx:close_idx]
    n = [0]

    def sub(m):
        n[0] += 1
        return "--gray-%s:%s" % (m.group(1), ramp[m.group(1)])

    body = re.sub(r"--gray-(\d+):\s*#[0-9a-fA-F]{6}", sub, body)
    return text[:open_idx] + body + text[close_idx:], n[0]


def find_blocks(text: str):
    """定位三处：:root、深色变量块、@media print 里的深色回退。

    按**内容特征**分类，而不是按位置：
      · :root                     —— 第一个顶层 :root
      · 深色变量块                —— 块内含 >=10 个 --gray- 声明，且不含
                                      --surface: #ffffff
      · @media print 的深色回退   —— 块内含 >=10 个 --gray- 声明，且**含**
                                      --surface: #ffffff（打印回退到白底）

    为什么不能按位置取：
      - 用 `re.search(...--gray-50:)` 再 `m.end()-1` 取 `{` 是错的，
        那个正则结束在 `--gray-50:` 的冒号上，取到的是冒号。
      - 用 `rfind('[data-bs-theme="dark"]')` 取「最后一个」也是错的：
        文件里 @media print 段在**前面**，后面还有一整段深色组件补丁，
        rfind 拿到的是补丁区的某条规则，不是变量块。
    按内容判，才不会随文件顺序变化而失效。
    """
    mask = comment_mask(text)

    # 1) :root
    m = re.search(r"^:root\s*\{", text, re.M)
    if not m:
        raise SystemExit("找不到 :root")
    a, b = block_span(text, m.end() - 1, mask)

    # 2 & 3) 扫描所有 [data-bs-theme="dark"] 块，按内容分类
    var_block = None
    print_block = None
    for mm in re.finditer(r'\[data-bs-theme="dark"\]', text):
        q = text.find("{", mm.end())
        if q == -1:
            continue
        try:
            o, c = block_span(text, q, mask)
        except SystemExit:
            continue
        body = text[o:c]
        if body.count("--gray-") < 10:
            continue                      # 组件补丁，不是变量块
        if "--surface: #ffffff" in body or "--surface:  #ffffff" in body:
            print_block = (mm.start(), o, c)
        elif var_block is None:
            var_block = (mm.start(), o, c)
    if var_block is None:
        raise SystemExit("找不到深色变量块")
    if print_block is None:
        raise SystemExit("找不到 @media print 的深色回退块")

    return (m.start(), a, b), var_block, print_block


targets = find_blocks(src)
print("找到三处：")
for name, (s0, o, c) in zip([":root（浅色）", "深色变量块", "@media print 回退"], targets):
    print("  %-18s 行 %d-%d" % (name, src.count("\n", 0, s0) + 1,
                                src.count("\n", 0, c) + 1))

# 从后往前替换，避免前面的偏移量失效
out = src
for idx, ramp in [(2, LIGHT), (1, DARK), (0, LIGHT)]:
    _s0, o, c = targets[idx]
    name = [":root（浅色）", "深色变量块", "@media print 回退"][idx]
    out, n = fill(out, o, c, ramp)
    print("  %-18s 写入 %d 个变量" % (name, n))

io.open(PATH, "w", encoding="utf-8", newline="").write(out)

# ---- 自检：逐块读回 ----
print()
print("=" * 70)
print("自检（从文件读回，确认每块拿到的是该拿的值）")
print("=" * 70)
chk = io.open(PATH, encoding="utf-8").read()
t2 = find_blocks(chk)
all_bad = False
for idx, want in [(0, LIGHT), (1, DARK), (2, LIGHT)]:
    name = [":root", "深色变量块", "@media print 回退"][idx]
    _s0, o, c = t2[idx]
    body = chk[o:c]
    got = dict(re.findall(r"--gray-(\d+):\s*(#[0-9a-fA-F]{6})", body))
    bad = {k: (got.get(k), v) for k, v in want.items() if got.get(k) != v}
    if bad:
        all_bad = True
    print("  %-18s %s" % (name, "OK" if not bad else "!! 不符 %s" % bad))

# 主色必须回到绿
if "--primary: #3d7a63" not in chk:
    all_bad = True
    print("  !! 主色不是 #3d7a63")
else:
    print("  %-18s OK" % "主色")

print()
print("完成" if not all_bad else "!! 有不符项")
sys.exit(1 if all_bad else 0)