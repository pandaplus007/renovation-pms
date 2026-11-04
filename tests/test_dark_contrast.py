"""深色主题对比度（**弱判据**）：所有文字与其实际背景的比值必须 >= 3:1。

⚠ 判据只用 3:1，比真标准松。**别把这个测试通过当成「对比度合格」。**
  3:1 是 WCAG 对**大号文字**（≥24px，或 ≥18.66px 且 600 字重）的要求，
  正文的硬线是 **4.5:1**。上一轮就是拿 3:1 当统一阈值，
  于是「深色 0 失败」这个结论里其实混着一批 3~4.5:1 的正文——
  合格是被放宽标准换来的。正文里 `.form-text` 3.32:1 就是这么漏过去的。

  真正的标准测试是 tests/test_contrast_wcag.py（按字号分档，
  正文 4.5 / 大字 3.0），那条才是判定依据。

  保留这个文件的价值：它是「有没有大面积元素**看不见**」的粗筛网，
  阈值低反而更容易发现极端情况（比如整个块底色没翻转）。
  两个都跑，别只看这个。

为什么必须自动化
----------------
深色主题最常见的翻车是「暗底 + 暗字」或「浅底 + 浅字」，成因永远是某处
写死了浅色背景（.card-header、.table-light、.navbar-light、.topbar 这类）。
它们在浅色主题下天衣无缝，一进深色就瞎。

而「能看见」和「达标」是两件事——低对比度的表现恰恰是「好像还行」，
肉眼扫一遍截图会漏掉大半。

判据用 WCAG 相对亮度算实际比值，不看 CSS 里写了什么：
    逐元素取 computed 的 color，往上找第一个非透明的 background-color
    作为实际底色，算比值，低于阈值即报。

⚠ 底色解析曾经是**简化做法**（只读 backgroundColor），v3 已改成
  与 test_contrast_wcag.py 同一套候选集逻辑（渐变各档独立作候选底，
  半透明层才叠加）。不改的话这个文件会报一堆假阳性 ——
  详见下面 bgCandidates 上面那段注释。
  所以渐变底的按钮会被误判成「白字压页面底色」——
  准确的版本见 test_contrast_wcag.py（它把渐变每一档当独立候选，
  且跳过「父底更亮」的白浮层）。
"""
from __future__ import annotations

import os
import sys
from collections import Counter

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8018"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "shots")

PAGES = [
    ("项目总览", "/"),
    ("待办总览", "/todo"),
    ("项目列表", "/projects"),
    ("变更签证", "/projects/1/changes"),
    ("待付账款", "/payables"),
    ("毛利报表", "/reports/profit"),
    ("合同详情", "/contracts/1"),
    ("变更编辑", "/changes/new"),
]

PROBE = r"""
() => {
  const parse = (c) => {
    const m = c.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/);
    return m ? [ +m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4] ] : null;
  };
  const lum = ([r, g, b]) => {
    const f = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92
        : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratio = (a, b) => {
    const la = lum(a), lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  };
  const over = (a, b) => [
    a[0]*a[3] + b[0]*(1-a[3]),
    a[1]*a[3] + b[1]*(1-a[3]),
    a[2]*a[3] + b[2]*(1-a[3]),
  ];
  const stopsOf = (bi) => {
    const out = [];
    const re = /#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)/g;
    let m;
    while ((m = re.exec(bi))) {
      const c = parse(m[0].length === 4
        ? '#' + m[0].slice(1).split('').map(ch => ch + ch).join('') : m[0]);
      if (c && c[3] > 0.01) out.push(c);
    }
    return out;
  };
  /* ⚠⚠ 原来只读 backgroundColor，结果把 .btn-primary 报成
     「近黑字压页面底 1.06:1」—— 假阳性。
     原因：主按钮的底走 background-image（linear-gradient），
     backgroundColor 是 rgba(0,0,0,0)，于是「往上找第一个
     alpha>0.5 的底」一路走到 <body> 的 #121212，
     完全跳过了按钮自己的青绿渐变。

     ⚠ 这不是把阈值调松就能解决的：v2 时按钮底是暗金渐变，
       白字压页面底有 15:1，所以假阳性一直没暴露。
       v3 按钮底换成亮蓝绿、文字换近黑字，假阳性立刻出现 ——
       **同一个 bug 藏了整整一轮，只因为两次配色恰好让它侥幸通过。**

     修法与 tests/test_contrast_wcag.py 的探针一致：
       不透明层（alpha>=0.999 的背景色或渐变色标）**各自**作为独立候选底；
       只有半透明层才叠上去；返回候选集，最后取最差的那个比值。
     渐变的第一档不能通吃 —— 它通常最容易达标，
     只测一端等于没测（这条教训 test_contrast_wcag.py 的文件头记着）。 */
  const bgCandidates = (el) => {
    const layers = [];
    let n = el;
    while (n && n !== document.documentElement) {
      const cs = getComputedStyle(n);
      const stops = stopsOf(cs.backgroundImage || '');
      const bc = parse(cs.backgroundColor);
      const opaque = [];
      for (const st of stops) if (st[3] >= 0.999) opaque.push(st);
      if (bc && bc[3] >= 0.999) opaque.push(bc);
      if (opaque.length) {
        const cands = opaque.slice();
        let base = opaque[0];
        for (let i = layers.length - 1; i >= 0; i--) base = over(layers[i], base);
        cands.push(base);
        return cands;
      }
      for (const st of stops) if (st[3] < 0.999) layers.push(st);
      if (bc && bc[3] > 0 && bc[3] < 0.999) layers.push(bc);
      n = n.parentElement;
    }
    let base = parse(getComputedStyle(document.body).backgroundColor) || [255,255,255,1];
    for (let i = layers.length - 1; i >= 0; i--) base = over(layers[i], base);
    return [base];
  };

  const out = [];
  const walk = (el) => {
    for (const n of el.childNodes) {
      if (n.nodeType === 3) {
        const t = n.textContent.trim();
        if (!t) continue;
        const cs = getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') continue;
        if (parseFloat(cs.opacity) < 0.5) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        const fg = parse(cs.color);
        if (!fg) continue;
        const bgs = bgCandidates(el);
        let worst = null;
        for (const bg of bgs) {
          const rt = Math.round(ratio(fg, bg) * 100) / 100;
          if (!worst || rt < worst.ratio) worst = { ratio: rt, bg };
        }
        out.push({
          tag: el.tagName.toLowerCase(),
          cls: String(el.className || '').slice(0, 56),
          size: Math.round(parseFloat(cs.fontSize) * 100) / 100,
          weight: parseInt(cs.fontWeight, 10),
          ratio: worst.ratio,
          fg: cs.color, bg: 'rgb(' + worst.bg.slice(0,3).map(Math.round).join(',') + ')',
          nbg: bgs.length,
          sample: t.slice(0, 16),
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

THRESHOLD = 3.0     # 大字标准。**不是正文标准**（正文是 4.5，见文件头）

rows = []
with sync_playwright() as pw:
    br = pw.chromium.launch()
    pg = br.new_page(viewport={"width": 1600, "height": 1100})
    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")
    pg.evaluate("localStorage.setItem('pms-theme','dark')")
    for name, path in PAGES:
        pg.goto(BASE + path)
        pg.wait_for_load_state("networkidle")
        for r in pg.evaluate(PROBE):
            r["page"] = name
            rows.append(r)
    pg.screenshot(path=os.path.join(OUT, "dark_audit.png"), full_page=True)
    br.close()

print("深色主题样本 %d 个" % len(rows))
low = [r for r in rows if r["ratio"] < THRESHOLD]
print("对比度 < %.1f:1 的：%d 个" % (THRESHOLD, len(low)))
print()
c = Counter()
sample = {}
for r in low:
    k = (r["tag"], r["cls"][:40], r["fg"], r["bg"])
    c[k] += 1
    sample.setdefault(k, r)
for (tag, cls, fg, bg), n in c.most_common(18):
    r = sample[(tag, cls, fg, bg)]
    print("  x%-3d %5.2f:1  %-22s %s" % (n, r["ratio"], tag + "." + cls, fg))
    print("         底色 %s   例(%s): %s" % (bg, r["page"], r["sample"]))

if not low:
    print()
    print("全部达标。")
sys.exit(1 if low else 0)