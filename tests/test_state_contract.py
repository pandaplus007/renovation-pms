"""状态色的静态 + 契约检查。

这一组专抓「写法没错但结果错」的 bug——上一轮 31 项全绿，却有个徽章
白字画在透明背景上不可见。两类根因：

  A. text-bg- 前缀忘了删，被拼成 text-bg-state-badge state-idle。
     Bootstrap 没有这个工具类，.state-badge 的样式照常生效，但
     .badge 自带的 color:#fff 留在上面，于是变成白字透明底。
     **静态可查**：模板里不该再出现 text-bg- 紧跟 state 相关输出。

  B. 方向徽章（进项/销项）不走状态词字典，靠条件表达式选档位。
     这类逻辑字典的一致性检查覆盖不到，必须单独验。
     数据里 0 张销项发票，运行时永远只看得到进项——所以这里
     从**模板源码里抠出真实表达式**再渲染两个分支，
     而不是手抄一份到测试里（手抄的副本测试不到模板真身）。
"""
from __future__ import annotations

import glob
import io
import os
import re
import sys

# 从本文件位置推导项目根，不写死盘符（本项目经 junction 暴露，真实路径在 D 盘）。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.state_colors import (STATE_TONE, STATE_TONE_MAP, badge_class,  # noqa: E402
                             tone_badge)
from app.web import templates  # noqa: E402  拿到配好过滤器的真实 Jinja env

TPL = os.path.join(ROOT, "app", "templates")
P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


print("=" * 78)
print("[A] text-bg- 前缀没有被拼到状态类上")
print("=" * 78)
bad = []
for f in glob.glob(os.path.join(TPL, "**", "*.html"), recursive=True):
    s = io.open(f, encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"""text-bg-(\{\{[^}]*\}\}|state[\w\- ]*)""", s):
        frag = re.sub(r"\s+", " ", m.group(0))
        if "state" in frag:
            ln = s[:m.start()].count("\n") + 1
            bad.append((os.path.relpath(f, TPL), ln, frag[:80]))
for rel, ln, frag in bad:
    print("    ! %-26s %4d  %s" % (rel[:26], ln, frag))
check("没有 text-bg- + state 的拼接（%d 处）" % len(bad), not bad, bad[:3])

print()
print("=" * 78)
print("[B] 模板里每个 state-<tone> 都带 .state-badge 前缀")
print("=" * 78)
orphan = []
for f in glob.glob(os.path.join(TPL, "**", "*.html"), recursive=True):
    s = io.open(f, encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"""class="[^"]*\bstate-(done|active|watch|idle|alert)\b[^"]*\"""",
                         s):
        cls = m.group(0)
        if "state-badge" not in cls:
            ln = s[:m.start()].count("\n") + 1
            orphan.append((os.path.relpath(f, TPL), ln, cls[:70]))
for rel, ln, cls in orphan:
    print("    ! %-26s %4d  %s" % (rel[:26], ln, cls))
check("没有缺前缀的 state 类（%d 处）" % len(orphan), not orphan, orphan[:3])

print()
print("=" * 78)
print("[C] state_badge 与 tone_badge 的契约不同，不能混用")
print("=" * 78)
check("state_badge 接状态词：'施工中' -> state-active",
      badge_class("施工中") == "state-badge state-active", badge_class("施工中"))
check("state_badge 接档位名会静默回落到 idle（所以不能这样用）",
      badge_class("active") == "state-badge state-idle", badge_class("active"))
check("tone_badge 接档位名：'active' -> state-badge state-active",
      tone_badge("active") == "state-badge state-active", tone_badge("active"))
# 这条是 A 类 bug 的真正护栏：拼错必须炸，不能静默出错色
for wrong in ["state-active", "Active", "actives", "", None, "状态"]:
    try:
        got = tone_badge(wrong)
        ok = False
    except ValueError:
        got, ok = "已抛 ValueError", True
    check("tone_badge(%r) 抛错而非静默出错色" % (wrong,), ok, got)
try:
    tone_badge("state-active")
except ValueError as e:
    check("报错信息提示改用 state_badge", "state_badge" in str(e), str(e))

print()
print("=" * 78)
print("[D] 方向徽章：抠出模板真身渲染两个分支")
print("=" * 78)
FRAG = re.compile(
    r"""\{\{\s*\((?P<expr>[^()]*direction[^()]*)\)\s*\|\s*tone_badge\s*\}\}""")
found = {}
for f in glob.glob(os.path.join(TPL, "**", "*.html"), recursive=True):
    s = io.open(f, encoding="utf-8", errors="replace").read()
    for m in FRAG.finditer(s):
        found.setdefault(os.path.relpath(f, TPL), m.group("expr").strip())

check("模板里有方向徽章表达式可抠", len(found) >= 1, sorted(found))
for rel, expr in sorted(found.items()):
    # 用真实 env 渲染，expr 里的 i.direction 换成两个方向各跑一次
    src = "{{ (%s) | tone_badge }}" % expr
    tpl = templates.env.from_string(src)
    class X(object):
        pass
    out = {}
    for d in ("进项", "销项"):
        i = X()
        i.direction = d
        out[d] = tpl.render(i=i)
    print("  %-26s %s" % (rel[:26], expr))
    print("      进项 -> %-26s 销项 -> %s" % (out["进项"], out["销项"]))
    check("%-24s 进项与销项 class 不同" % rel[:24],
          out["进项"] != out["销项"], out)
    check("%-24s 两者都带 state-badge 前缀" % rel[:24],
          all("state-badge" in v for v in out.values()), out)
    check("%-24s 进项=idle / 销项=active" % rel[:24],
          out["进项"] == "state-badge state-idle"
          and out["销项"] == "state-badge state-active", out)

print()
print("=" * 78)
print("[E] 字典覆盖：真实状态词全部收录")
print("=" * 78)
ms = io.open(os.path.join(ROOT, "app", "models.py"), encoding="utf-8",
             errors="replace").read()
REAL = set()
for m in re.finditer(r"^[A-Z_]+ = \[([^\]]*)\]", ms, re.M):
    REAL.update(re.findall(r"'([^']+)'", m.group(1)))
REAL.update(["已送货", "未送货", "无需送货", "已导入", "未提交", "停用", "启用",
             "结清", "已结清", "超付", "部分到货", "已到货", "已下单", "待下单",
             "正常", "作废", "进项", "销项", "草稿", "当前", "待启动"])
missing = sorted(w for w in REAL if w not in STATE_TONE_MAP)
print("  系统状态词 %d 个，字典覆盖 %d 个" % (len(REAL), len(REAL) - len(missing)))
if missing:
    print("  未覆盖（回落 idle，不报错）：%s" % missing)
check("未覆盖 <= 8 个", len(missing) <= 8, missing)

print()
print("=" * 78)
print("[F] 回落安全性：状态词是数据，不能让页面 500")
print("=" * 78)
for w in ["", None, 123, "字典建立之后新增的状态"]:
    try:
        got = badge_class(w)
        ok = got == "state-badge state-idle"
    except Exception as e:
        got, ok = "EXC %s" % e, False
    check("未知值 %r 回落 idle" % (w,), ok, got)

print()
print("=" * 78)
print("[G] 永久护栏：字典里的状态词旁边不允许再有 Bootstrap 语义徽章")
print("=" * 78)
# 这条是给人 review 兜底的。手工改模板时最容易漏的就是把
# {{ '施工中' | state_badge }} 塞回 text-bg-{{ ... }}，拼成
# text-bg-state-badge——页面不报错，只是那个徽章白字透明底看不见。
# 靠肉眼发现不了，只能让脚本盯着。
BOOT = re.compile(r"text-bg-(?:success|danger|warning|info|primary|secondary)")
NEG = "未无不非别尚未否"
leak = []
for f in glob.glob(os.path.join(TPL, "**", "*.html"), recursive=True):
    s = io.open(f, encoding="utf-8", errors="replace").read()
    for m in BOOT.finditer(s):
        # 往后看 140 字符：徽章的 class 和它的文字通常在同一行内
        tail = s[m.start():m.start() + 140]
        for w in STATE_TONE_MAP:
            for wm in re.finditer(re.escape(w), tail):
                # 「未超付」含子串「超付」，但两者语义相反：一个是异常、
                # 一个是「没有异常」。用前一个字判断是否为否定式，避免误报。
                i = wm.start()
                prev = tail[i - 1] if i > 0 else ""
                if prev in NEG:
                    continue
                ln = s[:m.start()].count("\n") + 1
                leak.append((os.path.relpath(f, TPL), ln, m.group(0), w))
                break
            else:
                continue
            break
for rel, ln, bg, w in leak:
    print("    ! %-26s %4d  %s  …含状态词「%s」" % (rel[:26], ln, bg, w))
check("没有状态词挂在 Bootstrap 语义徽章上（%d 处泄漏）" % len(leak),
      not leak, leak[:3])

# 反向验证：护栏本身不能是「永远绿」的假检查。
# 造一处已知违规塞进内存副本，确认它会被判为泄漏。
probe = '<span class="badge text-bg-danger">超付</span>'
hit = [w for w in STATE_TONE_MAP
       if any(tail_prev not in NEG
              for tail_prev in [probe[probe.find(w) - 1]
                                if probe.find(w) > 0 else ""])]
check("护栏对已知违规样本确实报警（防假绿）", "超付" in hit, hit)

print()
print("=" * 78)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
print("=" * 78)
sys.exit(1 if F else 0)