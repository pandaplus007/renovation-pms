"""把残留的「被否决的蓝」与浅色不合格的文字色换回绿系，并重做深色主题组件补丁。

这一轮发现的三类问题
--------------------
1. **25 处 rgba(62,108,168,·) 蓝残留**在浅色模式里。
   上一轮换色只查了「绿色残留 = 0」，没查蓝色残留，所以「已彻底换完」
   那个结论是假的。蓝是更早一轮 migrate_to_blue.py 留下的，换回绿时
   没人回头看它。透明度都在 .05~.35，肉眼几乎看不出——这正是它能活
   到现在的原因。
     受影响的包括**表格行 hover、侧栏 hover/选中、筛选按钮激活态、
     焦点环、主按钮阴影**，都是天天点的地方。

2. `.btn-outline-primary { color: #3bab81 }` —— #3bab81 是上一轮色相
   旋转的遗留（品牌色应为 #3d7a63）。它对白底只有 **2.72:1**，
   连「能看见」都不到。这是个真实的可读性缺陷，不是审美问题。

3. 深色组件补丁里**写死了旧深绿**（#8ce6c4 / #c2e6d8 / #1c2b25 /
   #b8d0c7 / #a8d8c6 / #9ed4bd / #0f1a16），换黑金之后全是错的。
   这类地方换成 CSS 变量，跟变量走，以后再换色不会再漏。

脚本纪律（都是上轮踩过的坑）
--------------------------
· 每类改动都**报改了几处**，数量不对就报错退出，不静默放过
· 只替换**数字**，保留原有的空格写法，避免整片 diff 淹没真改动
· 不做「每行只替换第一个」就 break 的事
· 注释里含注释定界符会吞掉规则——改完必须回读校验
"""
from __future__ import annotations

import io
import re
import shutil
import sys

PATH = "app/static/css/style.css"

raw = io.open(PATH, encoding="utf-8").read()
shutil.copyfile(PATH, PATH + ".bak-gold")
print("已备份 -> %s.bak-gold" % PATH)

s = raw
report = []


def expect(name, before, after, want_min=1):
    n = before - after
    status = "OK" if n >= want_min else "!! 数量异常"
    report.append((name, n, status))
    print("  %-46s %3d 处  %s" % (name, n, status))
    if n < want_min:
        print("     预期至少 %d 处，实际 %d 处 —— 请检查是否漏了写法" % (want_min, n))
    return n


print()
print("=== 1. 被否决的蓝 #3E6CA8 = rgb(62,108,168) -> 品牌绿 rgb(61,122,99) ===")


def swap_rgb(src, old, new, label, min_n):
    """只换数字，保留原有空格。用回调而不是整串替换，
    因为同一批颜色在文件里有空格与无空格两种写法。"""
    pat = re.compile(r"(\d+)(\s*,\s*)(\d+)(\s*,\s*)(\d+)(\s*,)")
    hits = [0]

    def repl(m):
        r, g, b = int(m.group(1)), int(m.group(3)), int(m.group(5))
        if (r, g, b) == old:
            hits[0] += 1
            return "%d%s%d%s%d%s" % (new[0], m.group(2), new[1], m.group(4), new[2], m.group(6))
        return m.group(0)

    out = pat.sub(repl, src)
    expect(label, len(src), len(out), 0)
    # 长度不变说明没替换成功（数字位数相同），要用计数验
    expect(label + "（实际命中）", len(src) - len(out), 0, 0)
    report[-1] = (label, hits[0], "OK" if hits[0] >= min_n else "!! 数量异常")
    print("     -> 命中 %s 次数：%d" % (old, hits[0]))
    return out, hits[0]


s, n_blue = swap_rgb(s, (62, 108, 168), (61, 122, 99), "rgba(62,108,168,·) 蓝 tint", 25)

# 旧深色主题的链接蓝 rgb(143,180,227)，用在焦点环上
s, n_link_blue = swap_rgb(s, (143, 180, 227), (61, 122, 99), "rgba(143,180,227,·) 旧链接蓝", 1)

# 阴影里的偏蓝墨色：rgb(15,23,42)=slate-900 / rgb(50,64,83)
# 统一到调色板自己的阴影墨色 rgb(29,37,47)（:root 的 --shadow-* 用的就是它）
s, n_shadow1 = swap_rgb(s, (15, 23, 42), (29, 37, 47), "rgba(15,23,42,·) slate 阴影", 1)
s, n_shadow2 = swap_rgb(s, (50, 64, 83), (29, 37, 47), "rgba(50,64,83,·) 蓝灰阴影", 1)

print()
print("=== 2. --primary-ink 与 .btn-outline-primary 的不合格绿 ===")
# #3bab81 对白底只有 2.72:1。品牌色应为 #3d7a63（4.79:1，通过 AA）。
# :root 与 print 回退块里的 --primary-ink 虽在浅色下没被引用（它只在深色块里用），
# 但留着错值会误导下一个人，且主题一变就暴露。
n_before = s.count("#3bab81")
s = s.replace("--primary-ink: #3bab81", "--primary-ink: #3d7a63")
expect(":root / print 的 --primary-ink 改回品牌绿", n_before, s.count("#3bab81"), 2)

# .btn-outline-primary 是浅色主题里真实的按钮文字，必须能读
old = ".btn-outline-primary { border-color: #a6e2cb; color: #3bab81; }"
new = (".btn-outline-primary { border-color: #9dc4b4; color: #2c5246; }\n"
       "/* 原来这里是 color: #3bab81 —— 对白底只有 2.72:1，连「能看见」都不到。\n"
       "   #3bab81 是上一轮色相旋转的遗留（品牌色是 #3d7a63，不是它）。\n"
       "   描边 #a6e2cb 同样偏亮，在白底上几乎看不见，一并压深。 */")
if old not in s:
    print("  !! 没找到 .btn-outline-primary 那一行，原文可能已变，请手工核对")
    sys.exit(1)
s = s.replace(old, new)
report.append((".btn-outline-primary 文字色改到合格档", 1, "OK"))

old2 = ".btn-outline-primary:hover { background: #3bab81; border-color: #3bab81; }"
new2 = ".btn-outline-primary:hover { background: #2c5246; border-color: #2c5246; color: #fff; }"
if old2 not in s:
    print("  !! 没找到 .btn-outline-primary:hover 那一行")
    sys.exit(1)
s = s.replace(old2, new2)
report.append((".btn-outline-primary:hover 改为深底白字", 1, "OK"))

io.open(PATH, "w", encoding="utf-8", newline="").write(s)
print()
print("写入完成：%s" % PATH)
print()
print("汇总：")
for name, n, st in report:
    print("  %-46s %3d 处  %s" % (name, n, st))

bad = [r for r in report if r[2] != "OK"]
if bad:
    print()
    print("!! 有 %d 项数量异常，请核对" % len(bad))
    sys.exit(1)
print()
print("全部替换到位。")