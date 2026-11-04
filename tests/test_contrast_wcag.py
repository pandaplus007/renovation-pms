"""对比度回归：两个主题下所有文字与其**实际**背景的比值必须达 WCAG AA。

为什么必须自动化，以及为什么要用真阈值
------------------------------------
1. 低对比度的表现恰恰是「好像还行」，肉眼扫截图会漏掉大半。
   上一轮 2029 个样本里 361 处不达标，靠肉眼看大概只能发现三四处。

2. ⚠ 上一版的阈值写的是 **3.0**，理由是「低于 3:1 一律报」。
   但 3:1 是 WCAG 对**大号文字**的要求（≥24px，或 ≥18.66px 且 600 字重）。
   正文的标准是 **4.5:1**。用 3:0 当统一阈值 = 把正文按大字标准放行，
   于是「深色 0 失败」这个结论里其实混着一批 3~4.5:1 的正文，
   合格是被放宽标准换来的，不是真的合格。
   本轮换成按字号分档的真实阈值。

3. 只测深色是不够的。本轮就发现浅色主题里 `.btn-outline-primary`
   的文字是 2.72:1，而深色那一轮一直是绿的。

4. 也因此必须覆盖**更多页面**：单页通过不代表全站通过。

判据不看 CSS 写了什么，只看 computed 的实际颜色：
    逐文本节点取 computed color，往上找第一个透明度 >0.5 的背景作为实际底色。
半透明底色**跳过**继续往上走——rgba(201,162,39,.16) 这种「金 + 低透明」
在深色下是设计意图，文字真正的参照面是它背后的卡片底。
（但「跳过」不等于「不用验」：叠加后的实际对比度会被本测试算出来，
  因为合成后的底色 = 前景之下的不透明祖先色 + 各层 rgba 混合。）
"""
from __future__ import annotations

import io
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
    ("项目详情", "/projects/1"),
    ("采购管理", "/procurement"),
    ("分包管理", "/subcontracts"),
    ("供应商", "/suppliers"),
    ("变更签证", "/projects/1/changes"),
    ("待付账款", "/payables"),
    ("发票管理", "/invoices"),
    ("毛利报表", "/reports/profit"),
    ("合同详情", "/contracts/1"),
    ("变更编辑", "/changes/new"),
    ("登录页", "/login"),
]

PROBE = r"""
() => {
  const parse = (c) => {
    const m = String(c).match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/);
    return m ? [ +m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4] ] : null;
  };
  const over = (a, b) => [   // 半透明层叠在底色上
    a[0]*a[3] + b[0]*(1-a[3]),
    a[1]*a[3] + b[1]*(1-a[3]),
    a[2]*a[3] + b[2]*(1-a[3]),
  ];
  const lum = ([r, g, b]) => {
    const f = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92
        : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratio = (a, b) => {
    const la = lum(a), lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  };

  /* 实际底色**候选集**（可能不止一个，取最差的那个比值）。

     ⚠⚠ 这里连着踩了两个坑，都在「渐变底」上：
     坑一：只读 backgroundColor。渐变走的是 background-image，
       .btn-primary / .nav-link.active / .progress-bar 都用 linear-gradient()，
       backgroundColor 是 rgba(0,0,0,0) → 探针一路往上找到页面底色，
       白字按钮被报成「白字压浅底 1.04:1」。这种假阳性会诱导人去
       「修」一个根本不存在的问题。

     坑二（更隐蔽）：把不透明层也塞进 layers 逆序合成，于是
       over(stop1, stop2) 在 alpha=1 时结果就是 stop1，
       **渐变的第一档永远赢**。而第一档通常正是最容易达标的那档：
         .btn-primary 白字压 linear-gradient(#3d7a63 → #6aa88c)
       只测到 #3d7a63 的 5.04:1（通过），完全没测 #6aa88c 那一半的
       2.77:1（不通过）→ 假绿，还以为探针没抓到。

     正确做法：不透明层（alpha≥0.999 的 backgroundColor 或渐变色标）
     各自作为**独立候选底**；只有半透明层才叠上去。
     文字横跨渐变时必须对每一档都成立，只测一端等于没测。 */
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

  const bgCandidates = (el) => {
    const layers = [];   // 半透明层，内层先收集
    let n = el;
    while (n && n !== document.documentElement) {
      const cs = getComputedStyle(n);
      const stops = stopsOf(cs.backgroundImage || '');
      const bc = parse(cs.backgroundColor);

      const opaque = [];
      for (const st of stops) if (st[3] >= 0.999) opaque.push(st);
      if (bc && bc[3] >= 0.999) opaque.push(bc);

      if (opaque.length) {
        const cands = opaque.slice();          // 每一档不透明色都是候选
        let base = opaque[0];                 // 半透明层叠在它之上
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
        /* 取最差的那个候选底——渐变两端都得过 */
        let worst = null;
        for (const bg of bgs) {
          const rt = Math.round(ratio(fg, bg) * 100) / 100;
          if (!worst || rt < worst.ratio) worst = { ratio: rt, bg };
        }
        const size = parseFloat(cs.fontSize);
        const weight = parseInt(cs.fontWeight, 10) || 400;
        /* WCAG「大号文字」= ≥24px，或 ≥18.66px 且 ≥600 字重 */
        const large = size >= 24 || (size >= 18.66 && weight >= 600);
        out.push({
          tag: el.tagName.toLowerCase(),
          cls: String(el.className || '').slice(0, 56),
          size: Math.round(size * 100) / 100,
          weight: weight,
          large: large,
          need: large ? 3.0 : 4.5,
          ratio: worst.ratio,
          fg: cs.color,
          bg: 'rgb(' + worst.bg.slice(0, 3).map(Math.round).join(',') + ')',
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

rows = []
report = []


def say(line=""):
    report.append(line)
    print(line)


with sync_playwright() as pw:
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": 1600, "height": 1100})
    pg = ctx.new_page()
    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    for theme in ("light", "dark"):
        pg.evaluate("t => localStorage.setItem('pms-theme', t)", theme)
        for name, path in PAGES:
            pg.goto(BASE + path)
            pg.wait_for_load_state("networkidle")
            try:
                got = pg.evaluate(PROBE)
            except Exception as e:                       # noqa: BLE001
                print("  !! %s %s 取样失败: %s" % (theme, path, e))
                continue
            for r in got:
                r["page"] = name
                r["theme"] = theme
                rows.append(r)
            pg.screenshot(path=os.path.join(OUT, "contrast_%s_%s.png" % (theme, name)))
    br.close()

light = [r for r in rows if r["theme"] == "light"]
dark = [r for r in rows if r["theme"] == "dark"]
say("样本：浅色 %d 个 / 深色 %d 个，合计 %d" % (len(light), len(dark), len(rows)))
say()
say("判据：正文 <18.66px 需 4.5:1；大字（≥24px 或 ≥18.66px 且 600 字重）需 3.0:1")
say()

all_low = []
for theme, rs in (("浅色", light), ("深色", dark)):
    low = [r for r in rs if r["ratio"] < r["need"]]
    all_low += low
    say("=== %s主题：不达标 %d / %d（%.1f%%）==="
        % (theme, len(low), len(rs), 100.0 * len(low) / max(1, len(rs))))
    c = Counter()
    sample = {}
    for r in low:
        k = (r["tag"], r["cls"][:44], r["fg"], r["bg"], r["need"])
        c[k] += 1
        sample.setdefault(k, r)
    # ⚠ 必须打印**全部**组，不能只打前 N。
    #   上一版打 most_common(24) 而实际有 33 组，于是 .btn-primary
    #   （白字压浅绿渐变 2.77:1）正好落在没显示的尾部，
    #   差点被当成「探针没测到」而放过。
    for (tag, cls, fg, bg, need), n in c.most_common(300):
        r = sample[(tag, cls, fg, bg, need)]
        say("  x%-3d %5.2f:1 (需 %.1f)  %s.%s"
            % (n, r["ratio"], need, tag, cls))
        say("         字色 %s   实际底 %s   %s / %s「%s」"
            % (fg, bg, r["theme"], r["page"], r["sample"]))
    say()

if all_low:
    say("不达标合计 %d 处（%d 组）"
        % (len(all_low), len({(r["theme"], r["tag"], r["cls"], r["fg"], r["bg"])
                              for r in all_low})))
else:
    say("全部达标。")

with io.open(os.path.join(OUT, "contrast_report.txt"), "w",
             encoding="utf-8", newline="") as f:
    f.write("\n".join(report) + "\n")

sys.exit(1 if all_low else 0)