"""状态色收敛的运行期验证：真实渲染 + 计算样式 + 对比度。

静态检查只能证明"写法统一"，证明不了"看起来对"。这里实测：
  1. 每个页面渲染后，状态徽章确实拿到了正确的档位 class
  2. 4 档配色的实际计算值符合预期（含背景与文字色）
  3. 对比度达 WCAG AA —— 这是上一轮发现 .text-warning 只有 1.63:1 的同款检查
  4. 同一状态词在不同页面渲染出的 class 相同（真正的一致性）
"""
from __future__ import annotations
import re
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8018"
P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


def _lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lum(rgb):
    r, g, b = [_lin(x) for x in rgb[:3]]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(c1, c2):
    l1, l2 = _lum(c1), _lum(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def rgb(s):
    return [int(float(x)) for x in re.findall(r"[\d.]+", str(s))[:3]]


JS_PROBE = r"""() => {
  const out = { badges: [], tones: {}, orphan: [] };
  const TONES = ['done','active','watch','idle','alert'];
  document.querySelectorAll('.state-badge, .state-done, .state-active, .state-watch, .state-idle, .state-alert')
    .forEach(el => {
      const cls = (typeof el.className === 'string') ? el.className : '';
      const tone = TONES.find(t => cls.includes('state-' + t));
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      if (r.width === 0) return;
      // 缺 .state-badge 前缀 = 只设了自定义属性、没拿到背景色声明
      if (!cls.includes('state-badge')) {
        out.orphan.push({ cls: cls.slice(0, 70), text: el.textContent.trim().slice(0, 12),
                          bg: cs.backgroundColor, fg: cs.color });
        return;
      }
      out.badges.push({
        tone, text: el.textContent.trim().slice(0, 12),
        fg: cs.color, bg: cs.backgroundColor,
        fontSize: cs.fontSize, weight: cs.fontWeight,
        bordered: cs.borderTopWidth,
      });
      out.tones[tone] = out.tones[tone] || { fg: cs.color, bg: cs.backgroundColor,
        size: cs.fontSize, weight: cs.fontWeight, n: 0, sample: el.textContent.trim().slice(0, 10) };
      out.tones[tone].n++;
    });
  return out;
}"""

PAGES = ["/", "/projects", "/projects/1", "/customers", "/customers/1",
         "/suppliers", "/invoices", "/approvals", "/todo", "/quotes",
         "/changes", "/sales-invoices", "/materials", "/expenses",
         "/contracts/1", "/subcontracts/1", "/reconcile/bank", "/inventory",
         "/purchase_orders" if False else "/procurement", "/oa/audit",
         "/purchases/1", "/reports/profit", "/payables"]

with sync_playwright() as pw:
    b = pw.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 950})
    pg = ctx.new_page()
    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    print("=" * 76)
    print("[1] 逐页渲染：徽章拿到正确档位，无渲染错误")
    print("=" * 76)
    all_tones = {}
    word2tone = {}
    orphans = []
    for p in PAGES:
        pg.goto(BASE + p)
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(240)
        r = pg.evaluate("""() => ({
          err: document.body.innerText.includes('Traceback')
               || document.body.innerText.includes('UndefinedError')
               || document.body.innerText.includes('jinja2'),
          plain: document.querySelectorAll('.text-bg-success, .text-bg-danger, .text-bg-warning').length,
        })""")
        d = pg.evaluate(JS_PROBE)
        # 孤立徽章（写了 state-<tone> 却漏了 .state-badge）会白字画在透明背景上，
        # 属于"渲染出来但看不见"，必须单独报出来而不是混进徽章统计。
        orphans.extend([(p, o) for o in d["orphan"]])
        check("%-20s 无报错，%d 个状态徽章" % (p, len(d["badges"])),
              not r["err"] and not d["orphan"], r["err"] or d["orphan"][:2])
        for t, v in d["tones"].items():
            all_tones.setdefault(t, dict(v))
            for bd in d["badges"]:
                if bd["tone"] == t and bd["text"]:
                    word2tone.setdefault(bd["text"], set()).add(bd["tone"])
        if r["plain"]:
            print("        提示：%d 处仍用 Bootstrap 原生徽章（非状态语义，保留）"
                  % r["plain"])

    print()
    print("=" * 76)
    print("[2] 4 档配色的实际计算值 + 对比度")
    print("=" * 76)
    for t in ["done", "active", "watch", "idle", "alert"]:
        if t not in all_tones:
            print("  %-8s （本轮无页面用到）" % t)
            continue
        v = all_tones[t]
        fg, bg = v["fg"], v["bg"]
        size = float(v["size"].replace("px", ""))
        # 徽章字号远小于 18.66px，无论字重多少都按普通文本的 4.5:1 要求。
        # 之前这里有个 large-text 分支，对 10.875px 的徽章永远不生效，
        # 属于写了但走不到的死逻辑，删掉免得日后误以为可以放宽。
        need = 4.5
        # 透明背景必须直接判失败。透明色 rgba(0,0,0,0) 的前三维是全 0，
        # 相对亮度算出来接近 0，和任何深色文字都能得到虚高的 21:1——
        # 这正是上一轮把「白字画在透明背景上」误判为通过的原因。
        bg_a = float(re.findall(r"[\d.]+", bg)[3]) if len(re.findall(r"[\d.]+", bg)) > 3 else 1.0
        if bg_a == 0:
            print("    %-7s 背景透明(%s)，无法判对比度 -> 判失败" % (t, bg))
            check("%-7s 背景不透明" % t, False, (fg, bg))
            continue
        cr = contrast(rgb(fg), rgb(bg))
        print("    %-7s bg=%-22s fg=%-22s %5.2f:1 需 %.1f  (%s %s) ×%d"
              % (t, bg, fg, cr, need, v["size"], v["weight"], v["n"]))
        check("%-7s 对比度 %.2f >= %.1f (WCAG AA)" % (t, cr, need), cr >= need,
              (fg, bg))

    print()
    print("  孤立徽章（缺 .state-badge，会白字透明底）：%d" % len(orphans))
    for p, o in orphans[:6]:
        print("    %-22s %s | bg=%s fg=%s" % (p[:22], o["text"][:10], o["bg"], o["fg"]))
    check("没有孤立徽章", not orphans, orphans[:3])

    print()
    print("=" * 76)
    print("[3] 一致性：同一状态词在任何页面都是同一档")
    print("=" * 76)
    conflict = {w: c for w, c in word2tone.items() if len(c) > 1}
    if conflict:
        for w, c in sorted(conflict.items()):
            print("    ! %-14s %s" % (w, sorted(c)))
    check("无同词异色（%d 个状态词已校验）" % len(word2tone), not conflict, conflict)

    # 进项/销项靠条件表达式选档位，不走状态词字典，字典的一致性检查管不到。
    # 这里在页面上单独验。注意数据现状：0 张销项发票，所以运行时永远只看得到进项；
    # 销项分支的契约由 test_state_contract.py 从模板源码渲染两个分支来验。
    print()
    dir_tones = {w: c for w, c in word2tone.items() if w in ("进项", "销项")}
    print("  进项/销项实际档位：%s"
          % {k: sorted(v) for k, v in dir_tones.items()})
    if "进项" in dir_tones:
        check("进项 = idle（中性灰，不代表比销项差）",
              dir_tones["进项"] == {"idle"}, dir_tones["进项"])
    if "销项" in dir_tones:
        check("销项 = active（实心绿，代表开出去的票）",
              dir_tones["销项"] == {"active"}, dir_tones["销项"])
        check("进项与销项档位不同（方向信息没丢）",
              dir_tones.get("进项") != dir_tones.get("销项"), dir_tones)
    else:
        print("  注：数据中 0 张销项发票，销项分支改由契约测试从模板源码渲染验证")

    print()
    print("=" * 76)
    print("[4] 信号强度：红色只在真问题上")
    print("=" * 76)
    alerts = [w for w, c in word2tone.items() if c == {"alert"}]
    print("  红色徽章显示的文字：%s" % alerts)
    # 允许的：超付、删除类审计动作、待办的严重档（其文字是异常类型名）
    check("红色未出现在普通业务状态上（如未收/待确认/草稿/已流失）",
          not any(w in ("未收", "待确认", "草稿", "已流失", "待审批", "洽谈中")
                  for w in alerts), alerts)

    print()
    print("=" * 76)
    print("[5] 每个徽章都带文字（不能只能靠颜色识别）")
    print("=" * 76)
    pg.goto(BASE + "/")
    pg.wait_for_load_state("networkidle")
    d = pg.evaluate(JS_PROBE)
    empty = [b for b in d["badges"] if not b["text"]]
    check("徽章均有可读文字（%d 个）" % len(d["badges"]), not empty, empty[:3])

    b.close()

print()
print("=" * 76)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
print("=" * 76)
sys.exit(1 if F else 0)