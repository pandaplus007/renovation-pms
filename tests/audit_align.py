"""对齐实测（修正版）。

上一版量错了：我比较的是表头文字与数据行的**绝对** y 坐标，
两者本来就不在同一行，差个行高是正常的，跟对齐无关。

真正要量的是**文字相对自己所在格子**的偏移：
    偏移 = 文字盒中心 − 格子中心
    · padding 上下相等 → 偏移 = 0（真正居中）
    · 上 padding 比下大 → 偏移为负（文字偏上）
表格逐列比 th 与 td 的偏移，就能看出表头和数据行是不是同一种对齐。
"""
from __future__ import annotations

import os
from collections import Counter

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8018"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = r"""
() => {
  const out = [];
  const textBox = (cell) => {
    let best = null;
    cell.childNodes.forEach(n => {
      if (n.nodeType === 3 && n.textContent.trim()) {
        const rg = document.createRange();
        rg.selectNodeContents(n);
        const b = rg.getBoundingClientRect();
        if (b.height > 0 && (!best || b.height > best.height)) best = b;
      }
    });
    if (best) return best;
    const kid = cell.querySelector('span,div,a,strong');
    return kid ? kid.getBoundingClientRect() : null;
  };
  const offset = (cell) => {
    const cb = cell.getBoundingClientRect();
    if (cb.height < 2) return null;
    const tb = textBox(cell);
    if (!tb || tb.height < 2) return null;
    return +(((tb.top + tb.height / 2) - (cb.top + cb.height / 2))).toFixed(2);
  };

  document.querySelectorAll('table.table').forEach((t, ti) => {
    const ths = [...t.querySelectorAll('thead th')].filter(h => h.offsetParent !== null);
    const tr = t.querySelector('tbody tr');
    if (!ths.length || !tr) return;
    const tds = [...tr.children];
    if (tds.length !== ths.length) return;      // colspan 表格跳过
    const thOff = ths.map(offset);
    const tdOff = tds.map(offset);
    const thPad = ths.map(h => {
      const cs = getComputedStyle(h);
      return [cs.paddingTop, cs.paddingBottom].join('/');
    });
    out.push({
      table: ti, cols: ths.length,
      thOff, tdOff, thPad,
      thBad: thOff.filter(v => v !== null && Math.abs(v) > 1).length,
      tdBad: tdOff.filter(v => v !== null && Math.abs(v) > 1).length,
      mismatch: thOff.filter((v, i) => v !== null && tdOff[i] !== null
                                   && Math.abs(v - tdOff[i]) > 1).length,
    });
  });
  return out;
}
"""

PAGES = [
    ("项目总览", "/"),
    ("待办总览", "/todo"),
    ("项目列表", "/projects"),
    ("变更签证-项目页", "/projects/1/changes"),
    ("变更签证-总览", "/changes"),
    ("采购合同详情", "/contracts/1"),
    ("待付账款", "/payables"),
    ("毛利报表", "/reports/profit"),
]

total_mismatch = 0
total_bad = 0
with sync_playwright() as pw:
    br = pw.chromium.launch()
    pg = br.new_page(viewport={"width": 1600, "height": 1100})
    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    for name, path in PAGES:
        pg.goto(BASE + path)
        pg.wait_for_load_state("networkidle")
        rows = pg.evaluate(PROBE)
        print("=" * 74)
        print(name)
        for r in rows:
            total_mismatch += r["mismatch"]
            total_bad += r["thBad"] + r["tdBad"]
            print("  表%d  %d 列" % (r["table"], r["cols"]))
            print("     表头偏移 %s" % r["thOff"])
            print("     数据偏移 %s" % r["tdOff"])
            print("     表头 padding %s" % Counter(r["thPad"]).most_common(2))
            if r["mismatch"]:
                bad = [(i, r["thOff"][i], r["tdOff"][i])
                       for i in range(r["cols"])
                       if r["thOff"][i] is not None and r["tdOff"][i] is not None
                       and abs(r["thOff"][i] - r["tdOff"][i]) > 1]
                print("     ✗ 表头与数据行对齐不一致的列：%s"
                      % ", ".join("第%d列(%s/%s)" % b for b in bad[:6]))
            else:
                print("     ✓ 表头与数据行对齐一致")
        print()

    br.close()

print("=" * 74)
print("合计：表头/数据自身未居中 %d 处，表头与数据行不一致 %d 列"
      % (total_bad, total_mismatch))