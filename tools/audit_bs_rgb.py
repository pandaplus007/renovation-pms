"""审计 Bootstrap 的 -rgb 变量：按**块**分别检查是否与所绑定的灰阶一致。

上一版有 bug：用 dict 存 -rgb，同名变量被**后出现的块覆盖**，
于是只看得到深色块的值，:root 里那些真正在生效的反而被遮住。
Bootstrap 的 -rgb 变量在两个块里各写一遍，必须分别验。

为什么这类错误值得专门查
------------------------
`:root` 里写了

    --bs-secondary-color: var(--gray-600);

看起来接好了，但 Bootstrap 的 `.text-secondary` 用的是

    color: RGBA(var(--bs-secondary-rgb))

——另一个变量，从没被覆盖。所以前一个是**死的**，改它零效果。
症状是「我明明改了颜色，怎么没变」，最难查的一种。
"""
from __future__ import annotations

import io
import re

PATH = "app/static/css/style.css"
s = io.open(PATH, encoding="utf-8").read()


def blocks():
    """列出所有顶层变量块：(名称, 起止偏移, 源码)。"""
    out = []
    for sel in (r":root", r'\[data-bs-theme="dark"\]'):
        for m in re.finditer(sel + r"\s*\{", s):
            st = m.end() - 1
            d, i = 0, st
            while i < len(s):
                if s[i] == "{":
                    d += 1
                elif s[i] == "}":
                    d -= 1
                    if d == 0:
                        break
                i += 1
            body = s[st : i + 1]
            ln = s[:st].count("\n") + 1
            out.append((sel.strip("[]").replace('"', ""), ln, body))
    return out


def hex_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


problems = []

for name, ln, body in blocks():
    body_off = s.index(body)
    print("=" * 74)
    print("块：%s（行 %d）" % (name, ln))
    print("=" * 74)

    # 本块里的 gray 定义
    grays = {}
    for m in re.finditer(r"--gray-(\d+)\s*:\s*#([0-9a-fA-F]{3,6})\b", body):
        grays[int(m.group(1))] = hex_rgb(m.group(2))

    # 本块里的 -rgb 定义
    rgbs = {}
    for m in re.finditer(r"--bs-([a-z0-9-]*?)rgb\s*:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", body):
        rgbs[m.group(1)] = ((int(m.group(2)), int(m.group(3)), int(m.group(4))),
                            s[: body_off + m.start()].count("\n") + 1)

    # 本块里绑定到 gray-N 的 --bs-*
    binds = []
    for m in re.finditer(r"--bs-([a-z0-9-]+)\s*:\s*var\(\s*--gray-(\d+)\s*\)", body):
        binds.append((m.group(1), int(m.group(2)),
                      s[: body_off + m.start()].count("\n") + 1))

    print("\n  本块 -rgb 变量 %d 个：" % len(rgbs))
    for k, (v, l) in sorted(rgbs.items()):
        print("    --bs-%srgb = %-18s 行 %d" % (k, str(v), l))

    print("\n  绑到 --gray-* 的 --bs-* 共 %d 个：" % len(binds))
    for name2, gn, l in binds:
        want = grays.get(gn)
        mark = ""
        if want is None:
            mark = "   (本块未定义 --gray-%d，用的是继承值)" % gn
        print("    --bs-%-28s -> --gray-%-3d %-18s 行 %d%s"
              % (name2, gn, str(want) if want else "?", l, mark))

    # Bootstrap 里哪些类用 -rgb 而不是 color 变量
    print()
    print("  Bootstrap 用 -rgb 形式取色的类（改 color 变量对它们无效）：")
    for cls, rgbvar in [
        (".text-secondary", "--bs-secondary-rgb"),
        (".text-body-secondary", "--bs-secondary-rgb"),
        (".btn-outline-secondary", "--bs-secondary-color-rgb"),
        (".text-muted", "--bs-secondary-color-rgb"),
    ]:
        key = rgbvar.replace("--bs-", "").replace("rgb", "")
        v = rgbs.get(key)
        if v is None:
            print("    %-24s 用 %-30s 本块未设置 → 沿用 Bootstrap 默认"
                  % (cls, rgbvar))
            problems.append((name, ln, rgbvar, None, "未设置"))
        else:
            print("    %-24s 用 %-30s = %-18s 行 %d" % (cls, rgbvar, str(v[0]), v[1]))

    print()

print("=" * 74)
print("需要修的")
print("=" * 74)
if problems:
    for blk, ln, var, val, why in problems:
        print("  [%s @行%d] %s —— %s" % (blk, ln, var, why))
else:
    print("  （无）")