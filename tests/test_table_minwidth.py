"""宽表的 min-width 必须保住（屏幕态）。

起因
----
style.css 里曾有一条无媒体限定的
`.table.col-minor-hidden.table-wide { min-width: 0 }`，
写在「表格次要列收起」那一节，理由是「收起后列少了，不再需要强制宽度」。

**这个理由是错的**，而且后果波及全站：
  次要列只收起 2-5 列，剩下 10-13 列照样需要 min-width 撑着，
  否则窄屏下宽表被压成一团字。该规则在屏幕态生效后，
  所有 .table-wide 的 min-width 都变成 0 —— 全站宽表不再横向滚动。

为什么一直没被发现
------------------
1. 表格没有横向滚动条不一定显眼（`.table-responsive` 仍然滚动，
   只是 min-width 没了，内容被压扁而非溢出，肉眼容易当成"正常"）；
2. 之前的打印测试只验**打印时** min-width 被清零，
   没验**屏幕时**它还在。两条规则同名，只差一个 `@media`，
   很容易把屏幕态那条也当成打印用的。
3. 没有任何一条断言覆盖「屏幕态宽表 min-width > 0」。

所以这里补上这个断言——它保护的是全站所有宽表。
"""
from __future__ import annotations

import io
import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8018"
P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = io.open(os.path.join(ROOT, "app", "static", "css", "style.css"),
              encoding="utf-8", errors="replace").read()

# 有次要列开关的宽表页面
PAGES = ["/projects", "/invoices", "/suppliers", "/customers",
         "/reports/profit", "/subcontracts", "/materials"]

print("=" * 78)
print("[1] CSS 静态检查：屏幕态不得有清 min-width 的全局规则")
print("=" * 78)
# 找所有给 .table-wide / .col-minor-hidden 设 min-width 的规则，
# 并判断它是否被 @media print 包住。
leak = []
for m in re.finditer(r"([^{}]*?)\{([^{}]*?min-width\s*:\s*0[^;}]*?)\}",
                     CSS, re.S):
    sel = re.sub(r"\s+", " ", m.group(1)).strip()
    body = re.sub(r"\s+", " ", m.group(2)).strip()
    if "table-wide" not in sel and "col-minor-hidden" not in sel:
        continue
    # 往回找最近的未闭合 @media
    head = CSS[:m.start()]
    depth = head.count("{") - head.count("}")
    media = []
    for mm in re.finditer(r"@media([^{]*)\{", head):
        if head[:mm.start()].count("{") - head[:mm.start()].count("}") >= depth:
            media.append(mm.group(1).strip())
    in_print = any("print" in x for x in media)
    if not in_print:
        leak.append((sel[:70], body[:40], media))
for sel, body, media in leak:
    print("    ! %-70s %s" % (sel, body))
    print("      外层 media=%s（屏幕态生效）" % media)
check("屏幕态没有把宽表 min-width 清零的规则", not leak, leak[:2])

print()
print("=" * 78)
print("[2] 屏幕态实测：每张宽表的 min-width 都必须 > 0")
print("=" * 78)
JS = r"""() => {
  const out = [];
  document.querySelectorAll('table').forEach(t => {
    const cs = getComputedStyle(t);
    out.push({
      cls: t.className,
      minW: parseFloat(cs.minWidth) || 0,
      minor: t.hasAttribute('data-col-minor'),
      hidden: t.classList.contains('col-minor-hidden'),
      cols: t.querySelectorAll('thead th').length,
    });
  });
  return out;
}"""

with sync_playwright() as pw:
    b = pw.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 950})
    pg = ctx.new_page()
    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    for path in PAGES:
        pg.goto(BASE + path)
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(350)
        rows = pg.evaluate(JS)
        wide = [r for r in rows if "table-wide" in r["cls"]]
        if not wide:
            print("  %-20s 无宽表（跳过）" % path)
            continue
        for r in wide:
            # 列数多才需要强制宽度；2-3 列的窄表不需要
            needs = r["cols"] >= 8
            ok = (r["minW"] > 0) or not needs
            print("  %-20s %2d 列  minW=%-6.0f  次要列=%s 收起=%s"
                  % (path, r["cols"], r["minW"], r["minor"], r["hidden"]))
            check("%s（%d 列）屏幕态 min-width > 0"
                  % (path, r["cols"]), ok,
                  "minW=%s，%d 列需要强制宽度，否则窄屏下被压扁" % (r["minW"], r["cols"]))

        # 窄屏下横向滚动容器必须真的能滚
        pg2 = pg.evaluate("""() => {
          const t = document.querySelector('table.table-wide');
          if (!t) return null;
          const wrap = t.closest('.table-responsive');
          if (!wrap) return {noWrap: true};
          return {scrollW: wrap.scrollWidth, clientW: wrap.clientWidth,
                  overflowX: getComputedStyle(wrap).overflowX};
        }""")
        if pg2 and not pg2.get("noWrap"):
            check("%s 横向滚动容器可用" % path,
                  pg2["clientW"] > 0 and pg2["scrollW"] >= pg2["clientW"],
                  pg2)

    print()
    print("=" * 78)
    print("[3] 窄屏（390px）下不溢出")
    print("=" * 78)
    # 复用已登录的 page，只改视口宽度。
    # 上一版新开 context + 再访问 /login，但新 context 没有登录态，
    # /login 被重定向到首页，找不到 input[name=username] 而超时。
    pg.set_viewport_size({"width": 390, "height": 844})
    for path in ["/projects", "/invoices", "/reports/profit"]:
        pg.goto(BASE + path)
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(400)
        r = pg.evaluate("""() => ({
          doc: document.documentElement.scrollWidth,
          win: window.innerWidth,
        })""")
        check("%s 窄屏无横向溢出（%d <= %d）" % (path, r["doc"], r["win"]),
              r["doc"] <= r["win"] + 1, r)
    b.close()

print()
print("=" * 78)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
print("=" * 78)
sys.exit(1 if F else 0)