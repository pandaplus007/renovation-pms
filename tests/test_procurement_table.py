"""采购管理页的订单明细表：不得有横向滚动条，且表头表体字号一致。

为什么单独立一条
----------------
这个页面的表只有 6 列、内容都很短，却长期挂着 `.table-wide`
（min-width:900px）。实测它比容器宽 3px，而 `.table-responsive` 是
`overflow-x:auto` —— **多 1px 就出滚动条**，于是屏幕上 48 张合同卡片
张张都挂一条横向滚动条。

这类问题服务端测不出来：HTTP 200、文本内容齐全、只是布局难看。
必须真的渲染出来量 `.table-responsive` 的 scrollWidth − clientWidth。
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
BASE = "http://127.0.0.1:8018"

WIDTHS = [1920, 1600, 1440, 1366, 1280]

F = []
P = []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


with sync_playwright() as pw:
    br = pw.chromium.launch()
    pg = br.new_page(viewport={"width": 1600, "height": 1100})
    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    # ---- 1. 页面本身要渲染出内容 ----
    pg.goto(BASE + "/procurement")
    pg.wait_for_load_state("networkidle")
    n = pg.locator('table[data-col-key="procurement"]').count()
    check("订单表已渲染（这一条挡「</style> 被删掉 → 整页被当 CSS 吞掉」）",
          n > 0, "表数量 = %d" % n)
    # 上一次踩坑：改 <style> 里的 CSS 时把闭合标签一起替换掉了，
    # 页面照常打开、标题正常、地址正常，只是正文一片空白，
    # 而只查 HTTP 200 与文本存在的服务端测试全绿。
    check("正文有内容（#result-box 存在）",
          pg.locator("#result-box").count() == 1)
    check("订单行有内容",
          pg.locator('table[data-col-key="procurement"] tbody tr').count() > 0)

    if n == 0:
        br.close()
        print("\n页面没渲染出来，后续断言无意义")
        sys.exit(1)

    # ---- 2. 各种视口宽度下都不得有横向滚动条 ----
    print("\n  横向溢出（.table-responsive 是 overflow-x:auto，多 1px 就出滚动条）")
    for w in WIDTHS:
        pg.set_viewport_size({"width": w, "height": 1000})
        pg.wait_for_timeout(200)
        worst = pg.evaluate(
            "() => Math.max(0, ...[...document.querySelectorAll('.table-responsive')]"
            ".map(e => e.scrollWidth - e.clientWidth))")
        check("%5dpx 视口下无横向溢出" % w, worst <= 0, "溢出 %spx" % worst)

    # ---- 3. 表头与表体字号一致 ----
    pg.set_viewport_size({"width": 1600, "height": 1100})
    pg.wait_for_timeout(200)
    sizes = pg.evaluate("""() => {
      const out = new Set();
      document.querySelectorAll('table[data-col-key="procurement"]').forEach(t => {
        const th = t.querySelector('thead th');
        const td = t.querySelector('tbody tr td');
        if (th) out.add(getComputedStyle(th).fontSize);
        if (td) out.add(getComputedStyle(td).fontSize);
      });
      return [...out];
    }""")
    check("表头与表体字号统一（原来表头 12px 而表体 14px，是反的）",
          len(sizes) == 1, "实测有 %s 几种" % sizes)

    # ---- 4. 列宽不再乱摊 ----
    # 自动布局下「状态」列拿到 316px 只为放一个 96px 的下拉框，
    # 而「采购单」列 325px 只放 7 个字。改 table-layout:fixed 后
    # 同一页多张卡片的列宽应当**完全一致**——不一致就说明还在自动摊。
    cols = pg.evaluate("""() => {
      const out = [];
      document.querySelectorAll('table[data-col-key="procurement"]').forEach(t => {
        const r = t.querySelector('tbody tr');
        if (!r) return;
        out.push([...r.children].map(c => Math.round(c.getBoundingClientRect().width)));
      });
      return out;
    }""")
    check("同一页所有订单表的列宽一致（说明用固定布局而非自动摊）",
          len({tuple(c) for c in cols}) == 1,
          "%d 种不同分配" % len({tuple(c) for c in cols}))

    mw = pg.evaluate(
        "() => getComputedStyle(document.querySelector"
        "('table[data-col-key=\"procurement\"]')).minWidth")
    check("不再有 min-width 强制撑宽", mw in ("0px", "0"), mw)

    br.close()

print()
if F:
    print("失败 %d 项：%s" % (len(F), "、".join(F)))
    sys.exit(1)
print("通过 %d 项，失败 0" % len(P))