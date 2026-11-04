"""找出在深色主题下**仍然是浅色**的元素。

为什么要单独写一条
------------------
本轮换深色主题时改了变量块、也改了组件补丁，但有几处是**硬编码的浅色背景**，
不受变量影响，于是在深色模式下一片浅绿留在了黑底上：

    客户：…           白字压浅绿  1.82:1
    已付：¥13,763,500 灰字压浅绿  2.33:1
    「施工中」徽章     浅绿字压浅绿底  1.19:1
    「阶段未开始」徽章  1.23:1

这类问题**对比度测试能报出来，但看不出「为什么」**——报告只给字色与
底色，不告诉你那条底色是哪来的。所以要专门扫一遍：把所有深色下仍然亮的面
挑出来，直接指出元素与它的 background。

判据：深色主题下，元素的实际背景（含渐变色标）若相对亮度 > 0.10，
      就说明「这块该是深色却是浅色」。
      （0.10 大约相当于 #585757，比任何合理的深色表面都亮。）

**不是**「亮底就错」——弹窗、下拉、白底打印预览确实该是浅的。
所以只报「自己就是最亮层」的元素：若父底更亮，说明它只是一个白浮层
压在深底上，那是正常的。

⚠ 但深色主题**本来就有几个刻意很亮的强调色**：
     --accent-deep  #28776f（选中行/侧栏高亮）
     --accent-bar   #298e83（进度条/焦点环）
     --accent-full  #67beb5（主按钮 / tab 选中 / 日期选择器选中日）
     --primary-grad 的两端
     --sidebar-active 的两端
   它们不是「漏网的浅色」，是设计意图。所以 ALLOWED 里显式列出——
   加新强调色时要在这里登记，这样「忘记登记」本身也会被发现。
   登记的来源是 style.css 的深色块，不是硬编码色值，色板改了这里自动跟随。

   ⚠⚠ 但**变量名写错不报错**，只会让白名单静默失效：
     v2→v3 把 --gold-deep / --gold-bar / --on-gold / --on-gold-bright
     改名成 --accent-* 之后，这个列表还留着旧名字 ——
     于是「白名单一个强调色都收集不到」，它们全被判成漏网。
     这种失败反而是好事（会红），真正危险的是反向：
     有人为了让它变绿而放宽判据，白名单就形同虚设了。
     所以下面加了一条自检：名单里每个名字都必须真的在深色块里定义过。

写法坑：用 forEach 回调时 `continue` 是语法错误（函数体不是循环体），
会抛 "Illegal continue statement"。这里用普通 for 循环。
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8018"

# ---------- 深色主题里「刻意很亮」的强调色（来自 style.css 的深色块） ----------
# 解析方式：直接在浏览器里问「深色块声明的这些变量是什么颜色」，
# 不在本文件里硬编码色值——色板一改，这里自动跟随，不用同步维护两份。
ALLOWED_VARS = ["--accent-deep", "--accent-bar", "--accent-full",
                "--primary-grad", "--sidebar-active",
                "--on-accent", "--on-accent-bright",
                "--success-ink", "--warning-ink", "--danger-ink", "--info-ink"]

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
    ("合同详情", "/contracts/1"),
]

PROBE = r"""
(vars) => {
  const parse = (c) => {
    const t = String(c).trim();
    /* 必须同时认 hex 与 rgb()：变量声明里写的是 #8a6720，
       而 computed style 给的是 rgb(138, 103, 32)。只认 rgb() 的话
       白名单会一个都收集不到 —— 于是「强调色」全被判成漏网，
       测试变成永远失败，且报出来的东西看着完全合理。 */
    const m = t.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/);
    if (m) return [ +m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4] ];
    const h = t.match(/^#([0-9a-fA-F]{3,8})$/);
    if (h) {
      let x = h[1];
      if (x.length === 3 || x.length === 4) x = x.split('').map(ch => ch + ch).join('');
      return [ parseInt(x.slice(0, 2), 16), parseInt(x.slice(2, 4), 16),
               parseInt(x.slice(4, 6), 16), 1 ];
    }
    return null;
  };

  /* 把 CSS 变量声明里出现的所有颜色抠出来，当作「本主题认可的亮色」。
     只在 dark 主题下取值，所以读到的就是深色块声明的那些。
     ⚠ 这段必须放在 parse 定义**之后**：原来写在前面，
       const 存在暂时性死区，直接抛 "Cannot access 'parse' before
       initialization"——页面元素一个都没扫到，看起来像「没有漏网」。 */
  const allowed = new Set();
  for (const v of vars) {
    const raw = getComputedStyle(document.documentElement).getPropertyValue(v).trim();
    const re = /#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)/g;
    let m;
    while ((m = re.exec(raw)) !== null) {
      const s = m[0].length === 4
        ? '#' + m[0].slice(1).split('').map(ch => ch + ch).join('') : m[0];
      const c = parse(s);
      if (c) allowed.add(c.slice(0, 3).map(Math.round).join(','));
    }
  }

  const lum = ([r, g, b]) => {
    const f = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92
        : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const brightest = (cs) => {
    let own = 0;
    const bc = parse(cs.backgroundColor);
    if (bc && bc[3] >= 0.5) own = Math.max(own, lum(bc));
    const bi = cs.backgroundImage || '';
    const re = /#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)/g;
    let m;
    while ((m = re.exec(bi)) !== null) {
      const raw = m[0];
      const c = parse(raw.length === 4
        ? '#' + raw.slice(1).split('').map(ch => ch + ch).join('') : raw);
      if (c && c[3] >= 0.5) own = Math.max(own, lum(c));
    }
    return own;
  };

  const out = [];
  const all = document.querySelectorAll('body *');
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 24) continue;
    if (r.width * r.height < 3000) continue;

    const own = brightest(cs);
    if (own <= 0.10) continue;

    // 父底更亮 → 只是个白浮层压在深底上，正常
    let par = 0;
    let n = el.parentElement;
    while (n && n !== document.documentElement && par <= own) {
      const pc = parse(getComputedStyle(n).backgroundColor);
      if (pc && pc[3] >= 0.9) par = Math.max(par, lum(pc));
      n = n.parentElement;
    }
    if (par > own) continue;

    // 是不是深色块里**明确声明过的强调色**？是就放行。
    // 逐个色标比：渐变里只要有一个色标不在白名单，就算可疑。
    const raw = (cs.backgroundColor || '') + ' ' + (cs.backgroundImage || '');
    const re = /#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)/g;
    let m, allKnown = true, seenAny = false;
    while ((m = re.exec(raw)) !== null) {
      const s = m[0].length === 4
        ? '#' + m[0].slice(1).split('').map(ch => ch + ch).join('') : m[0];
      const c = parse(s);
      if (!c || c[3] < 0.5) continue;
      seenAny = true;
      if (!allowed.has(c.slice(0, 3).map(Math.round).join(','))) allKnown = false;
    }
    if (seenAny && allKnown) {
      out.push({
        tag: el.tagName.toLowerCase(),
        cls: String(el.className || '').slice(0, 60) + '  [已登记的强调色]',
        w: Math.round(r.width), h: Math.round(r.height),
        lum: Math.round(own * 1000) / 1000,
        bg: cs.backgroundColor,
        bi: (cs.backgroundImage || '').slice(0, 78),
        accent: true,
      });
      continue;
    }

    out.push({
      tag: el.tagName.toLowerCase(),
      cls: String(el.className || '').slice(0, 60),
      w: Math.round(r.width), h: Math.round(r.height),
      lum: Math.round(own * 1000) / 1000,
      bg: cs.backgroundColor,
      bi: (cs.backgroundImage || '').slice(0, 78),
      accent: false,
    });
  }
  return out;
}
"""

hits = {}
accents = {}
with sync_playwright() as pw:
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": 1600, "height": 1100})
    pg = ctx.new_page()
    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")
    pg.evaluate("localStorage.setItem('pms-theme','dark')")
    for name, path in PAGES:
        pg.goto(BASE + path)
        pg.wait_for_load_state("networkidle")
        for r in pg.evaluate(PROBE, ALLOWED_VARS):
            k = (r["tag"], r["cls"], r["bg"], r["bi"])
            bucket = accents if r.get("accent") else hits
            if k not in bucket:
                bucket[k] = dict(r)
                bucket[k]["pages"] = set()
            bucket[k]["pages"].add(name)
    br.close()

print("=== 已登记的强调色（预期亮，不算漏网）%d 组 ===" % len(accents))
for k, r in sorted(accents.items(), key=lambda kv: -kv[1]["lum"]):
    print("  %-8s.%-46s 亮度 %.3f  %dx%d"
          % (r["tag"], r["cls"][:46], r["lum"], r["w"], r["h"]))

# ---- 自检：ALLOWED_VARS 必须真的解析得出颜色 ------------------------
# 不加这条的话，名单里写错一个变量名只是「少收一个颜色」，
# 报告看起来和正常情况一模一样（强调色那栏少几行而已）。
# v2→v3 换名之后就是这样静默失效了一轮。
print()
if not accents:
    print("!! 自检失败：白名单一个颜色都没解析出来 —— ALLOWED_VARS 里的变量名")
    print("   与深色块对不上（换过名？），这份白名单现在形同虚设。")
    print("   真正危险的不是这里报错，而是有人为了让它变绿去放宽判据。")
    sys.exit(2)
print("自检：白名单解析出 %d 组强调色（非 0，名单与深色块对得上）" % len(accents))
print()
print("=== 漏网的浅色面 %d 组 ===" % len(hits))
print()
for k, r in sorted(hits.items(), key=lambda kv: -kv[1]["lum"]):
    print("  %-8s.%-44s 亮度 %.3f  %dx%d"
          % (r["tag"], r["cls"][:44], r["lum"], r["w"], r["h"]))
    print("           bg=%s" % r["bg"])
    if r["bi"] not in ("none", ""):
        print("           渐变=%s" % r["bi"])
    print("           出现在：%s" % "、".join(sorted(r["pages"])))

sys.exit(1 if hits else 0)