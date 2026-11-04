"""排版实测审计：把关键页面上所有可见文字的「渲染后」尺寸/字重/行高/
字间距/对齐方式全部抓下来，按角色归类，找出不符合规范的部分。

为什么用渲染值而不是扫 CSS：
  CSS 里写的 font-size 经常被 Bootstrap 的 `.small`、`.fs-*`、栅格工具类
  或浏览器默认值覆盖。扫源码只能看到「意图」，这里要的是「结果」。
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8018"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "shots")

# Carbon productive type set（基准 14px）。这是「产品界面」该用的那一半，
# expressive 那套（16px 基准、fluid 大标题）是给营销页面的。
CARBON = {
    "label-01":         dict(px=12, lh=16, w=400, ls=0.32),
    "body-compact-01":  dict(px=14, lh=18, w=400, ls=0.16),
    "body-01":          dict(px=14, lh=20, w=400, ls=0.16),
    "heading-compact-01": dict(px=14, lh=18, w=600, ls=0.16),
    "heading-01":       dict(px=14, lh=20, w=600, ls=0.16),
    "heading-03":       dict(px=20, lh=28, w=400, ls=0),
    "heading-04":       dict(px=28, lh=36, w=400, ls=0),
}

PROBE = r"""
() => {
  const out = [];
  const seen = new Set();
  const walk = (el) => {
    for (const n of el.childNodes) {
      if (n.nodeType === 3) {
        const txt = n.textContent.trim();
        if (!txt) continue;
        const cs = getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') continue;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        const key = el.tagName + '|' + cs.fontSize + '|' + cs.fontWeight
                  + '|' + cs.lineHeight + '|' + cs.letterSpacing + '|' + cs.textAlign;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({
          tag: el.tagName.toLowerCase(),
          cls: (el.className && el.className.baseVal !== undefined
                ? el.className.baseVal : String(el.className || '')).slice(0, 70),
          size: parseFloat(cs.fontSize),
          weight: parseInt(cs.fontWeight, 10),
          lh: cs.lineHeight === 'normal' ? null : parseFloat(cs.lineHeight),
          ls: cs.letterSpacing === 'normal' ? 0 : parseFloat(cs.letterSpacing),
          align: cs.textAlign,
          family: cs.fontFamily.split(',')[0].replace(/["']/g, ''),
          sample: txt.slice(0, 18),
        });
        continue;
      }
      if (n.nodeType === 1) walk(n);
    }
  };
  walk(document.body);
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
    ("供应商付款编辑", "/supplier-payments/1/edit"),
    ("变更签证编辑", "/changes/new"),
    ("审批流程配置", "/approvals/flows"),
]

allrows = []
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
        for r in rows:
            r["page"] = name
            allrows.append(r)
        print("%-18s %3d 种文字样式" % (name, len(rows)))
    br.close()

print()
print("=" * 78)
print("渲染后字号分布（共 %d 个样本）" % len(allrows))
print("=" * 78)
by_size = Counter(r["size"] for r in allrows)
for size, n in sorted(by_size.items()):
    ok = "✓规范" if size in (12, 14, 16, 18, 20, 24, 28) else "✗ 不在规范"
    print("  %5.1fpx  x%-4d  %s" % (size, n, ok))

print()
print("字号种类：%d 种（Carbon productive 只该用 12/14/16/20/24/28 六档）"
      % len(by_size))
odd = sorted(s for s in by_size if s not in (12, 14, 16, 20, 24, 28))
print("越界字号：%s" % odd)

print()
print("=" * 78)
print("字重分布")
print("=" * 78)
for w, n in sorted(Counter(r["weight"] for r in allrows).items()):
    ok = "✓" if w in (400, 600) else "✗ 规范只用 400/600（+大标题 300）"
    print("  %3d  x%-4d %s" % (w, n, ok))

print()
print("=" * 78)
print("字间距（letter-spacing）")
print("=" * 78)
ls = Counter(r["ls"] for r in allrows)
print("  全部为 0 的样本：%d / %d" % (ls.get(0, 0), len(allrows)))
print("  Carbon 要求 12px → +0.32px，14px → +0.16px（正文不能为 0，")
print("  小字号不加字间距中文会糊成一团）")

print()
print("=" * 78)
print("行高：是否与字号配套")
print("=" * 78)
bad_lh = [r for r in allrows
          if r["lh"] and r["size"] in (12, 14)
          and (r["lh"] / r["size"] < 1.15 or r["lh"] / r["size"] > 1.75)]
print("  正文(12/14px)行高比在 1.15~1.75 之外的：%d 个" % len(bad_lh))
seen = set()
for r in bad_lh:
    k = (r["size"], r["lh"], r["cls"][:40])
    if k in seen:
        continue
    seen.add(k)
    print("    %5.1fpx / lh %-6s  .%s" % (r["size"], r["lh"], r["cls"]))

print()
print("=" * 78)
print("对齐方式")
print("=" * 78)
for a, n in sorted(Counter(r["align"] for r in allrows).items()):
    print("  %-8s x%d" % (a, n))

print()
print("=" * 78)
print("字体族")
print("=" * 78)
for f, n in Counter(r["family"] for r in allrows).most_common():
    print("  %-22s x%d" % (f, n))

print()
print("=" * 78)
print("最常见的 25 种组合（tag.class -> size/weight/ls）")
print("=" * 78)
combos = Counter()
sample = {}
for r in allrows:
    k = (r["tag"], r["cls"], r["size"], r["weight"], r["ls"])
    combos[k] += 1
    sample.setdefault(k, r["sample"])
for (tag, cls, size, w, l), n in combos.most_common(25):
    print("  %-42s %5.1fpx w%-3d ls%-5s  例:%s"
          % (("%s.%s" % (tag, cls))[:42], size, w, l, sample[(tag, cls, size, w, l)]))

out = os.path.join(OUT, "type_audit.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(allrows, f, ensure_ascii=False, indent=1)
print()
print("原始数据：%s" % out)