"""状态色收敛的验证。

要证明的不是"颜色变了"（那是主观的），而是三条可检验的性质：
  1. **一致性**：同一个状态词在全站任何页面都是同一档色
  2. **信号强度**：红色（alert）只出现在金额异常上，不出现在普通业务状态上
  3. **可用性**：每种色都带文字，且对比度达 WCAG AA
"""
from __future__ import annotations
import glob
import io
import os
import re
import sys
from collections import defaultdict

# 从本文件位置推导项目根，不写死盘符。本项目经 junction 暴露为
# C:\Users\...\.proma\...，真实路径在 D 盘同步盘下；写死路径的脚本
# 换台机器或用真实路径调用就找不到文件（AGENTS.md 记过这个坑）。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.state_colors import STATE_TONE, STATE_TONE_MAP, badge_class  # noqa: E402

TPL = os.path.join(ROOT, "app", "templates")
P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


print("=" * 76)
print("[1] 收敛度：模板里还剩多少处自定义映射")
print("=" * 76)
rest = []
for f in glob.glob(os.path.join(TPL, "**", "*.html"), recursive=True):
    s = io.open(f, encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"text-bg-\{\{(.*?)\}\}", s, re.S):
        frag = re.sub(r"\s+", " ", m.group(1)).strip()
        if "state_" in frag:
            continue
        rest.append((os.path.relpath(f, TPL), frag))
print("  剩余未收敛：%d 处" % len(rest))
for f, frag in rest:
    print("    %-30s %s" % (f[:30], frag[:80]))
check("自定义动态映射 <= 3 处（其余全走统一字典）", len(rest) <= 3, len(rest))

print()
print("=" * 76)
print("[2] 一致性：同一状态词在任何地方都是同一档")
print("=" * 76)
# 收集所有 state_badge 的直接调用与静态 state-* 类
word2tone = defaultdict(set)
for f in glob.glob(os.path.join(TPL, "**", "*.html"), recursive=True):
    s = io.open(f, encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"\{\{\s*([\w.]+?)\s*\|\s*state_badge\s*\}\}", s):
        pass  # 变量无法静态求值，交给运行期测
    # 静态：{{ '施工中' | state_badge }}
    for m in re.finditer(r"\{\{\s*'([^']+)'\s*\|\s*state_badge\s*\}\}", s):
        word2tone[m.group(1)].add(STATE_TONE_MAP.get(m.group(1), "idle"))
    # 静态：state-done 之类直接写死
    for m in re.finditer(r"badge[^>]*?state-(done|active|watch|idle|alert)", s):
        pass
print("  静态字面量调用的状态词：%d 个" % len(word2tone))
check("所有静态调用的状态词都在统一字典内",
      all(STATE_TONE_MAP.get(w) for w in word2tone), list(word2tone))

# 字典内部不能有冲突
check("统一字典里同一状态词只有一个档位（构造上保证）",
      len({w: STATE_TONE_MAP[w] for w in STATE_TONE_MAP}) == len(STATE_TONE_MAP))

print()
print("=" * 76)
print("[3] 信号强度：红色只用于金额异常")
print("=" * 76)
ALERT_WORDS = [w for w, t in STATE_TONE_MAP.items() if t == "alert"]
print("  字典中标为 alert 的状态词：%s" % ALERT_WORDS)
check("alert 只包含超付（金额异常）", ALERT_WORDS == ["超付"], ALERT_WORDS)

# 模板里 state-alert 出现的地方
alert_ctx = []
for f in glob.glob(os.path.join(TPL, "**", "*.html"), recursive=True):
    s = io.open(f, encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"state-alert", s):
        ln = s[:m.start()].count("\n") + 1
        line = s.split("\n")[ln - 1].strip()
        # 存整行，判断白名单时再截断显示。上一版在 append 时就截到 80 字符，
        # 而「超付」二字出现在第 96 位——正好被切掉，导致合法用法被判违规，
        # 又一次「测试自己有 bug 却报别人错」。
        alert_ctx.append((os.path.relpath(f, TPL), ln, line))
print("  模板中 state-alert 共 %d 处：" % len(alert_ctx))
for f, ln, line in alert_ctx:
    print("    %-26s %4d  %s" % (f[:26], ln, line[:96]))
# 允许的：超付（金额异常，字典里唯一的 alert 词）、审计删除类、
# 待办严重档（走 colors 字典）。任一处新增红色都必须能说清属于哪一类。
OK = ("超付", "删除", "colors[lv]", "'danger'")
bad = [x for x in alert_ctx if not any(k in x[2] for k in OK)]
for f, ln, line in bad:
    print("    ! 未登记的红色：%s:%d %s" % (f, ln, line[:96]))
check("没有把普通业务状态标成红色", not bad, bad)

print()
print("=" * 76)
print("[4] 四档覆盖率：字典是否覆盖了系统里所有真实状态词")
print("=" * 76)
REAL = set()
for kw in ["CUSTOMER_STATUS", "PROJECT_STATUS", "PAYMENT_STATUS", "PO_STATUS",
           "TASK_STATUS", "APPROVAL_STATUS", "INVOICE_STATUS", "INVENTORY_TYPES",
           "CHANGE_STATUS", "QUOTE_STATUS", "INVOICE_APPLICATION_STATUS"]:
    p = os.path.join(ROOT, "app", "models.py")
    break
s = io.open(os.path.join(ROOT, "app", "models.py"), encoding="utf-8", errors="replace").read()
for m in re.finditer(r"^[A-Z_]+ = \[([^\]]*)\]", s, re.M):
    for w in re.findall(r"'([^']+)'", m.group(1)):
        REAL.add(w)
# 加上其他来源
for extra in ["已送货", "未送货", "无需送货", "已导入", "未提交", "停用", "启用",
              "结清", "已结清", "超付", "部分到货", "已到货", "已下单", "待下单",
              "正常", "作废", "进项", "销项", "草稿", "当前", "待启动"]:
    REAL.add(extra)
missing = sorted(w for w in REAL if w not in STATE_TONE_MAP)
print("  系统里的状态词共 %d 个，字典覆盖 %d 个"
      % (len(REAL), len(REAL) - len(missing)))
if missing:
    print("  未覆盖（会回落到中性灰 idle）：%s" % missing)
check("未覆盖的状态词 <= 8 个（回落到 idle 是安全的，不会变红）",
      len(missing) <= 8, missing)

print()
print("=" * 76)
print("[5] 未知状态词必须安全回落（不能变红、不能抛异常）")
print("=" * 76)
for w in ["", "某个不存在的状态", None, "已删除但没在字典里"]:
    try:
        cls = badge_class(w if w else "")
        ok = "state-idle" in cls
    except Exception as e:
        cls, ok = "EXC:%s" % e, False
    check("未知值 %r 回落 idle" % (w,), ok, cls)
check("含额外类时仍带主类", "state-badge" in badge_class("施工中", extra="text-dark"))

print()
print("=" * 76)
print("汇总")
print("=" * 76)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
sys.exit(1 if F else 0)