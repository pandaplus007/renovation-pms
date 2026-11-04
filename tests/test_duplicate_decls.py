"""同一规则块内的**重复变量声明**（值不同）必须为 0。

这个 bug 真实发生过，值得记下来
----------------------------
v3 把深色主题从金色换成蓝绿，改完所有对比度测试全绿，
但截图整屏**还是金色**。原因是深色主变量块里有**两份**同样这八条的声明：

    --accent-deep / --accent-bar / --accent-full / --on-accent /
    --on-accent-bright / --primary-grad / --primary-grad-hover / --on-active-grad

第二份是 v2 金色那一轮追加的，位置更靠后，于是把 v3 的值整个盖掉 ——
`--primary-grad` 变回金渐变，`--on-active-grad` 变回白字。
而 `--accent` / `--primary-ink` 只声明了一份，所以它们是新的蓝绿。
于是出现「变量一半新一半旧、截图全金」这个自相矛盾的现象。

为什么三个既有测试都没抓到
------------------------
1. tests/test_css_structure.py —— 只看语法。重复声明语法完全正确。
2. tests/test_print_theme.py —— 只看「变量有没有在 print 块复位」，不看值。
3. tests/test_contrast_wcag.py —— **它确实报错了**（深色 27 处不达标），
   但报出来的数是「白字压金渐变 4.80:1」这种**看起来已经达标**的数，
   人看到只会去想「哪里文字没取到变量」，
   不会想到「变量本身被另一条声明覆盖了」。
   ⚠ 最后是靠「把 computed 的 --primary-grad 和文件里的字面值对一下」
   才定位到的 —— 而这一步没有任何自动化。

判据与实现要点
--------------
同一个规则块（花括号深度 1）里，同一个自定义属性出现两次、且**值不同**，即为问题。

实现：逐字符状态机 + 块栈。两个地方最容易写错，都踩过：

1. `--name` 有三种出现位置，只有第一种是「声明」：
       --name: value;          ← 声明
       var(--name)             ← 引用
       background: var(--name) inset
   只在「刚 ; 或 { 之后」那个 --name 才算声明。
   不判这个的话 `color: var(--primary)` 里的 --primary 被当成声明，
   报告全是噪音，跑三次都得不出「0 处」这种能下结论的输出。

2. 选择器文本不能靠「记录上一个分隔符的位置」反推。
   那样遇到嵌套规则（@media 里套 dark 块）会把选择器算成 't' / 'g' 这种
   单字符 —— 第一版就是这样，结果**一个 :root 块都没认出来**，
   测试「通过」了但其实什么都没检查。
   改成「深度 0 时把字符攒进 pending，遇 { 结算为选择器」，
   嵌套规则自然被排除在外。

为什么允许「值相同」的重复
------------------------
真重复且值相同时删掉一份也无害，但先放过：
误报一次就足以让人养成「这个测试不可信」的习惯。
只有**值不同**才报 —— 那才是静默失效的真条件。
"""
from __future__ import annotations

import io
import re
import sys

PATH = "app/static/css/style.css"
s = io.open(PATH, encoding="utf-8").read()


def strip_comments(t: str) -> str:
    """去掉注释，但**保留换行**，让行号仍对得上原文件。

    为什么必须保留换行：报错要给行号给人定位，压成一行行号全错。
    CSS 注释不嵌套，遇到第一个 */ 就结束 —— 不能用正则切。
    """
    out, i, n = [], 0, len(t)
    while i < n:
        if t.startswith("/*", i):
            j = t.find("*/", i + 2)
            if j < 0:
                break
            out.append("\n" * t.count("\n", i, j))
            i = j + 2
        else:
            out.append(t[i])
            i += 1
    return "".join(out)


t = strip_comments(s)

NAME = re.compile(r"--[a-z0-9-]+")

blocks = []            # (块 id, 选择器, 起始行)
decls = {}             # 块 id -> [(行, 名, 值)]
stack = []             # 当前所在块的 id 栈
depth = 0
line = 1
pending = []           # 深度 0 时攒选择器文本
at_decl_start = True
i, n = 0, len(t)

while i < n:
    ch = t[i]

    if ch == "\n":
        line += 1
        # ⚠ 换行**不能**清掉 at_decl_start：几乎每条声明都是「{ 或 ; 之后
        #   换行 + 缩进 + --name」。第一版在这里清了 flag，
        #   于是 4 个主题块只认出 5 条直属声明（实际 120+），
        #   而输出「0 处重复」看起来和「真的没问题」一模一样。
        if depth == 0:
            pending.append(ch)
        i += 1
        continue

    # ---- 声明：只在「刚 ; 或 { 之后」的 --name 才算 ----
    if at_decl_start and ch == "-" and t[i:i + 2] == "--" and stack:
        m = NAME.match(t, i)
        j = t.find(":", i, i + 80)
        if m and j > 0:
            name = m.group(0)
            k = j + 1
            while k < n and t[k] != ";":
                k += 1
            val = re.sub(r"\s+", "", t[j + 1:k]).lower()
            if depth == len(stack):              # 直属当前块
                decls.setdefault(stack[-1], []).append((line, name, val))
            i = k
            continue

    if ch == "{":
        sel = re.sub(r"\s+", " ", "".join(pending).strip())
        bid = len(blocks)
        blocks.append((bid, sel, line))
        stack.append(bid)
        depth += 1
        pending = []
        at_decl_start = True
        i += 1
        continue

    if ch == "}":
        depth -= 1
        if stack:
            stack.pop()
        at_decl_start = True
        pending = []
        i += 1
        continue

    if ch == ";":
        at_decl_start = True
        pending = []
        i += 1
        continue

    if depth == 0:
        pending.append(ch)
    elif not ch.isspace():
        at_decl_start = False
    i += 1

# ---- 自检：主题块与声明条数必须合理，否则这份检查等于没跑 ----------
# 这个自检是必需的：**「0 处重复」既可能是真的没问题，也可能是解析器坏了。**
# 本项目已经两次栽在这上面：
#   第一版 选择器提取用「反推上一个分隔符」，一个 :root 都没认出来；
#   第二版 换行时清了 at_decl_start，4 个主题块只认出 5 条声明。
# 两次都输出「0 处重复」，和真的没问题完全一样。
theme_blocks = [b for b in blocks if b[1] in (":root", '[data-bs-theme="dark"]')]
theme_decls = sum(len(decls.get(b[0], [])) for b in theme_blocks)
if len(theme_blocks) < 3 or theme_decls < 80:
    print("!! 解析器自检失败")
    print("   主题块认出 %d 个（至少 3）、直属声明 %d 条（至少 80）"
          % (len(theme_blocks), theme_decls))
    print("   这份测试现在等于没跑 —— 先修解析器，别把「解析失败」"
          "读成「没有重复声明」。")
    sys.exit(2)

# ---- 报告 ------------------------------------------------------------
bad = []
for bid, sel, bln in blocks:
    ds = decls.get(bid)
    if not ds:
        continue
    seen = {}
    for ln, name, val in ds:
        if name in seen and seen[name][1] != val:
            bad.append((sel, bln, name, seen[name][0], seen[name][1], ln, val))
        seen.setdefault(name, (ln, val))

total_theme_decls = sum(len(decls.get(b[0], [])) for b in theme_blocks)
print("规则块 %d 个；主题块 %d 个（直属声明合计 %d 条）"
      % (len(blocks), len(theme_blocks), total_theme_decls))
print("值不同的重复声明 %d 处" % len(bad))
for sel, bln, name, pl, pv, cl, cv in bad:
    print("")
    print("  块 %s（行 %d）" % (sel[:64], bln))
    print("    %-22s 行%-6d %s" % (name, pl, pv[:56]))
    print("    %-22s 行%-6d %s   ← 靠后这份生效，前一份被静默盖掉"
          % ("", cl, cv[:56]))

print("")
if bad:
    print("!! %d 处重复声明（值不同）" % len(bad))
    sys.exit(1)
print("无重复声明。")
