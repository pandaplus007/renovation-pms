"""CSS 结构体检：抓那些「页面照旧、服务端测试全绿，但样式没生效」的问题。

为什么需要它
------------
本轮在 style.css 里踩了一个坑，值得立个永久防线：

  我在解释「为什么 Bootstrap 的 .small 是坏的」的注释里，**原样引用了**
  Bootstrap 的定义并带上行内注释：

      .small { font-size: .875em }   ← 这里本来写的是 /* 父级… */

  CSS 注释**不嵌套**。外层注释被正文里那个定界符提前闭合，
  后面的内容全部变成活 CSS 被解析。于是我那条「把 .small 钉死成 12px」
  的规则**根本没被解析**，页面继续按 .875em 渲染 12.25px——
  而 HTML 里压根没出现 font-size 字样，所有服务端测试都是绿的。

这类问题的共同特征是：**它不会让程序变慢、不会报错、不会 500**，
只是悄悄不起作用。所以只能靠读数检查。

检查项
------
1. 注释里出现定界符（嵌套注释）
2. 注释未闭合 / 闭合数与开启数不符
3. 声明缺分号（会被并进下一条，值被丢弃）
4. font-size 用了相对单位（em/%/rem 里的 em 与 %）——刻度要求绝对 px
5. 括号不配对
"""
from __future__ import annotations

import io
import re
import sys

FILES = ["app/static/css/style.css"]

fail = 0


def bad(msg: str):
    global fail
    fail += 1
    print("  [FAIL] " + msg)


def ok(msg: str):
    print("  [PASS] " + msg)


for path in FILES:
    css = io.open(path, encoding="utf-8").read()
    print("=" * 74)
    print(path, "（%d 行）" % (css.count("\n") + 1))
    print("=" * 74)

    # ---- 1 & 2. 注释嵌套 / 未闭合 ----
    print("\n[1] 注释结构")
    i, n, line_no, nested = 0, len(css), 1, []
    while i < n:
        if css.startswith("/*", i):
            start_line = line_no
            end = css.find("*/", i + 2)
            if end == -1:
                bad("第 %d 行开始的注释没有闭合" % start_line)
                break
            body = css[i + 2:end]
            if "/*" in body:
                nested.append((start_line, body[:90].replace("\n", " / ")))
            i = end + 2
            line_no += css.count("\n", start_line and 0 or 0, i)
            line_no = css.count("\n", 0, i) + 1
            continue
        i += 1
    if nested:
        for ln, body in nested:
            bad("第 %d 行的注释内部还有 /* —— 注释不嵌套，定界符会提前闭合"
                % ln)
            print("         正文: %s" % body)
    else:
        ok("没有嵌套注释")
    o, c = css.count("/*"), css.count("*/")
    (ok if o == c else bad)("注释定界符配对：/* x%d  */ x%d" % (o, c))

    # ---- 3. 声明缺分号 ----
    print("\n[2] 声明是否缺分号")
    # 找出「值 + 换行 + 下一条属性名」——合并后会静默丢弃前一条的值
    missing = re.findall(
        r"([\w-]+)\s*:\s*[^;{}\n]+?\n\s*(?=[-a-z]+\s*:)", css)
    # 上面这个正则会把多行值误报，只取真正可疑的（含颜色/尺寸关键字）
    real = [m for m in missing
            if re.search(r"#[0-9a-f]{3,8}|px|rem|%", m)]
    if real:
        for m in real[:6]:
            bad("疑似缺分号：%s 后面直接跟了下一条声明" % m)
    else:
        ok("未发现缺分号的声明")

    # ---- 4. 相对字号 ----
    print("\n[3] 字号是否用绝对 px")
    # 先剥掉注释再找：注释里**必然**会出现 .875em 之类的字样
    #（本文件就在注释里引用了 Bootstrap 的定义来说明它为什么坏）。
    # 把它们算成违规，等于让「写清楚为什么」这件事本身变成过错，
    # 下次就只能删掉解释——那才是真的损失。
    code_only = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                       css, flags=re.S)
    rel = []
    for m in re.finditer(r"font-size:\s*([0-9.]+)(em|%|ex|ch)\b", code_only):
        ln = code_only[:m.start()].count("\n") + 1
        rel.append((ln, m.group(0)))
    if rel:
        for ln, txt in rel:
            bad("第 %d 行  %s —— 相对单位，父级一变就级联出新的尺寸"
                % (ln, txt))
    else:
        ok("活代码里所有 font-size 都是 px（注释内的举例不计）")

    # ---- 5. 括号配对 ----
    print("\n[4] 花括号配对")
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    okd, cl = stripped.count("{"), stripped.count("}")
    (ok if okd == cl else bad)("{ x%d  } x%d" % (okd, cl))

    # ---- 6. 属性选择器的值必须带引号 ----
    # [href^=/projects/] 是**无效选择器**，解析器会整条丢弃该规则：
    # 不报错、不警告、页面照旧渲染，只是那条规则凭空消失。
    # 本项目踩过一次——深色主题里项目名链接的覆盖规则就是这么没了的，
    # 症状是「深色下亮底亮字、对比度 1.43:1」，而服务端测试全绿。
    print("\n[5] 属性选择器引号")
    naked = re.findall(r"\[[a-zA-Z-]+[\^$*~|]?=[^\"'\s\]]+\]", stripped)
    if naked:
        for sel in naked:
            bad("属性选择器缺引号，整条规则会被解析器丢弃：%s" % sel)
    else:
        ok("所有属性选择器的值都带引号")

    # ---- 7. linear-gradient 圆括号配平 ----
    # 批量替换脚本里给正则末尾加右括号时条件写错，漏掉过 2 处，
    # 结果是 --sidebar-active 的值变成 `linear-gradient(90deg, a, b;`
    print("\n[6] linear-gradient 圆括号")
    bad_grad = [g for g in re.findall(r"linear-gradient\([^;{}]*;?", stripped)
                if g.count("(") != g.count(")")]
    if bad_grad:
        for g in bad_grad[:4]:
            bad("linear-gradient 括号不配平：%s" % g[:70])
    else:
        ok("linear-gradient 括号都配平")

print()
print("=" * 74)
print("结构问题 %d 处" % fail)
sys.exit(1 if fail else 0)