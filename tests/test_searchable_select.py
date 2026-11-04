"""可搜索下拉：功能与回归闸门。

这个测试盯的东西分三类，每一类都对应一种**已经发生过的**坏法：

1. **该有的没有** —— 长列表下拉没有被接管（等于功能没上线，
   但页面完全正常，肉眼扫一遍看不出来）。
2. **不该有的有了** —— 3~5 项的短下拉也套了输入框。
   这是**过度应用**，比漏掉更烦人：选第 2 项要先点开再打字。
   而且它不会报错，只会让整个界面显得笨重，所以必须自动挡。
3. **接管了但坏了** —— 打开面板后选项不显示 / 搜索不过滤 /
   选完不提交 / 深色下面板是块白斑。

还有一条独立于本功能的闸门：
   模板里 inline <script> 的**语法**必须能被浏览器解析。
   这条是从 materials/list.html 的真实事故来的 ——
   一个跨行的单引号字符串让整个 script 块不执行，
   而症状是「点批量删除报 is not defined」，看起来像按钮坏了。
"""
from __future__ import annotations

import io
import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8018"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "shots")

# 选项数达到这个值才接管。必须与 static/js/searchable-select.js 里的
# MIN_OPTIONS 一致 —— 两处各写各的就会出现「测试说该有、页面上没有」
# 或者反过来，而两边都看不出对方存在。
MIN_OPTIONS = 8

# 漏斗多选里数据行少于这个数就不加搜索框（与 flt-search.js 一致）
FLT_MIN_ROWS = 4

PAGES = [
    ("项目总览", "/"),
    ("待办总览", "/todo"),
    ("项目列表", "/projects"),
    ("项目详情", "/projects/1"),
    ("采购管理", "/procurement"),
    ("分包管理", "/subcontracts"),
    ("供应商", "/suppliers"),
    ("材料库", "/materials"),
    ("待付账款", "/payables"),
    ("发票管理", "/invoices"),
    ("合同列表", "/contracts/list"),
    ("库存流水", "/inventory/records"),
    ("OA 审核", "/oa/audit"),
    ("项目表单", "/projects/new"),
    ("发票表单", "/invoices/new"),
    ("采购单表单", "/purchases/new"),
    ("材料表单", "/materials/new"),
    ("变更表单", "/changes/new"),
    ("合同详情", "/contracts/1"),
]

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  " + detail) if detail else ""))


# 探针：接管情况 / 面板可用性 / 语法错误
PROBE = r"""
() => {
  const all = [...document.querySelectorAll('select')];
  const enhanced = all.filter(s => s.closest('.ss'));
  const native = all.filter(s => !s.closest('.ss'));
  return {
    total: all.length,
    enhancedCount: enhanced.length,
    // 每个被接管的：选项数、是否在表单内、是否有 name（表单能不能提交）
    enhanced: enhanced.map(s => ({
      name: s.name || '',
      id: s.id || '',
      n: s.options.length,
      inForm: !!s.closest('form'),
      hasToggle: !!s.parentElement.querySelector('.ss-toggle'),
      hasSearch: !!s.parentElement.querySelector('.ss-search'),
      label: (s.parentElement.querySelector('.ss-label') || {}).textContent || '',
    })),
    // 每个没被接管的：记录原因（测试要能区分「合理跳过」与「该接管却漏了」）
    native: native.map(s => ({
      n: s.options.length,
      name: s.name || '',
      reason: s.multiple ? 'multiple'
            : s.size > 1 ? 'size'
            : s.hasAttribute('data-no-search') ? 'no-search'
            : s.classList.contains('flatpickr-monthDropdown-months') ? 'flatpickr'
            : s.closest('.flatpickr-current-month, .flatpickr-calendar') ? 'flatpickr'
            : '阈值以下',
    })),
    fltSearch: document.querySelectorAll('.ms-search').length,
    // 漏斗多选里「数据行数」（排除全选行）
    fltMenus: [...document.querySelectorAll('.ms-menu')].map(m => ({
      rows: m.querySelectorAll('.form-check:not(.ms-all)').length,
      hasSearch: !!m.querySelector('.ms-search'),
    })),
  };
}
"""

# 面板交互探针：打开 → 输词 → 看剩下几项 → 选中 → 看原生 select 的值
PANEL = r"""
(selName) => {
  const sel = [...document.querySelectorAll('select')].find(s => s.name === selName);
  if (!sel) return { err: '找不到 select' };
  const wrap = sel.closest('.ss');
  if (!wrap) return { err: '没被接管' };
  const btn = wrap.querySelector('.ss-toggle');
  btn.click();
  const panel = wrap.querySelector('.ss-panel');
  if (panel.hidden) return { err: '点了没打开' };
  const list = panel.querySelector('.ss-list');
  const before = [...list.children].filter(n => !n.hidden).length;
  const search = panel.querySelector('.ss-search');
  // 取第 2 个可见项的文字，输入它的前两个字
  const vis = [...list.children].filter(n => !n.hidden);
  const target = vis[Math.min(1, vis.length - 1)];
  const word = (target.textContent || '').trim().slice(0, 2);
  search.value = word;
  search.dispatchEvent(new Event('input', { bubbles: true }));
  const after = [...list.children].filter(n => !n.hidden).length;
  const r = {
    before: before, after: after, word: word,
    searchWorks: after > 0 && after < before,
    panelBg: getComputedStyle(panel).backgroundColor,
    optColor: getComputedStyle(vis[0]).color,
    searchPlaceholderColor: getComputedStyle(search).color,
    // 面板位置：必须完全在视口内（fixed 定位算错就会溢出屏幕）
    rect: panel.getBoundingClientRect().toJSON(),
    vw: window.innerWidth, vh: window.innerHeight,
  };
  // 选中第一个可见项
  const pick = [...list.children].filter(n => !n.hidden)[0];
  pick.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  r.closedAfterPick = panel.hidden;
  r.newValue = sel.value;
  r.newLabel = wrap.querySelector('.ss-label').textContent;
  return r;
}
"""

with sync_playwright() as pw:
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": 1500, "height": 1050})
    pg = ctx.new_page()
    page_errors = {}
    pg.on("pageerror", lambda e: page_errors.setdefault(pg.url, str(e)))

    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    print("=" * 74)
    print("[1] inline <script> 语法（整块死掉的闸门）")
    print("=" * 74)
    for label, path in PAGES:
        pg.goto(BASE + path)
        try:
            pg.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        bad = pg.evaluate(r"""() => {
          const out = [];
          for (const s of document.querySelectorAll('script')) {
            if (s.src) continue;
            const t = s.textContent;
            if (!t.trim()) continue;
            try { new Function(t); } catch (e) { out.push(String(e).slice(0, 90)); }
          }
          return out;
        }""")
        check("%-12s 无脚本语法错误" % label, not bad,
              ("；".join(bad)) if bad else "")

    print()
    print("=" * 74)
    print("[2] 接管规则：长列表要接管，短列表不许接管")
    print("=" * 74)
    total_enh, total_native, leak = 0, 0, []
    over = []
    for label, path in PAGES:
        pg.goto(BASE + path)
        try:
            pg.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        d = pg.evaluate(PROBE)
        total_enh += d["enhancedCount"]
        total_native += len(d["native"])
        for s in d["native"]:
            if s["reason"] == "阈值以下" and s["n"] >= MIN_OPTIONS:
                leak.append((label, s["name"], s["n"]))
        for s in d["enhanced"]:
            if s["n"] < MIN_OPTIONS:
                over.append((label, s["name"], s["n"]))
        if d["enhanced"]:
            miss = [s["name"] or s["id"] for s in d["enhanced"]
                    if not s["hasToggle"] or not s["hasSearch"]]
            check("%-12s 被接管的 %d 个都带搜索框" % (label, d["enhancedCount"]),
                  not miss, ("缺：" + ",".join(miss)) if miss else "")
        # 原生 select 必须仍在 form 里、且有 name —— 否则提交会少字段
        bad = [s for s in d["enhanced"] if s["inForm"] and not s["name"] and not s["id"]]
        if bad:
            check("%-12s 被接管的原生 select 仍可提交" % label, False,
                  str(len(bad)) + " 个既无 name 也无 id")

    check("没有「该接管却漏掉」的下拉（选项 %d+ 却保持原生）" % MIN_OPTIONS,
          not leak, str(leak[:6]))
    check("没有「不该接管却接管了」的下拉（选项 <%d）" % MIN_OPTIONS,
          not over, str(over[:6]))
    print("  （合计接管 %d 个，原生保留 %d 个）" % (total_enh, total_native))

    print()
    print("=" * 74)
    print("[3] 漏斗多选（.ms-menu）的搜索框")
    print("=" * 74)
    menus = 0
    bad_ms = []
    for label, path in PAGES:
        pg.goto(BASE + path)
        try:
            pg.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        d = pg.evaluate(PROBE)
        for m in d["fltMenus"]:
            menus += 1
            want = m["rows"] >= FLT_MIN_ROWS
            if want != m["hasSearch"]:
                bad_ms.append((label, m["rows"], "应有" if want else "不该有"))
    check("漏斗多选共 %d 个，数据行 >=%d 的都有搜索框" % (menus, FLT_MIN_ROWS),
          not bad_ms, str(bad_ms[:6]))

    print()
    print("=" * 74)
    print("[3b] 筛选菜单默认必须是收起的")
    print("=" * 74)
    # 这一条是被真实事故逼出来的：为了把菜单排成 flex 便于滚动，
    # 我写了 `.ms-searchable { display: flex }`，特异度 (0,2,0) 盖掉了
    # Bootstrap 的 `.dropdown-menu { display: none }` (0,1,0)，
    # 于是**所有筛选菜单一直摊在页面上**互相叠着。
    # 症状是「一进页面就看到三四个下拉糊在内容上」——
    # 会被误判成 z-index / overflow 问题，往完全错的方向查。
    always_open = []
    for label, path in PAGES:
        pg.goto(BASE + path)
        try:
            pg.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        d = pg.evaluate(r"""() => {
          const bad = [];
          for (const m of document.querySelectorAll('.ms-menu')) {
            if (getComputedStyle(m).display === 'none') continue;
            // 父级 .dropdown 被折叠（.filter-wrap.is-collapsed）不算
            const wrap = m.closest('.filter-wrap');
            if (wrap && wrap.classList.contains('is-collapsed')) continue;
            const r = m.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) bad.push(m.className.slice(0, 40));
          }
          return bad;
        }""")
        if d:
            always_open.append((label, d))
    check("没有筛选菜单在未点击时就可见", not always_open, str(always_open[:4]))

    print()
    print("=" * 74)
    print("[4] 面板能打开、能过滤、能选中、能落回原生 select")
    print("=" * 74)
    pg.goto(BASE + "/inventory/records")
    pg.wait_for_load_state("networkidle")
    r = pg.evaluate(PANEL, "material_id")
    check("库存流水 / 材料（192 项）：面板能打开", r.get("err") is None, r.get("err", ""))
    if r.get("err") is None:
        check("  搜索确实缩小了结果（%d -> %d，关键词「%s」）"
              % (r["before"], r["after"], r["word"]), r["searchWorks"])
        check("  选中后面板关闭", r["closedAfterPick"])
        check("  原生 select 的值已更新", bool(r["newValue"]) and r["newValue"] != "",
              "value=%s 按钮显示=%s" % (r["newValue"], r["newLabel"]))
        rc = r["rect"]
        check("  面板完整落在视口内（fixed 定位没算错）",
              rc["left"] >= 0 and rc["top"] >= 0
              and rc["right"] <= r["vw"] + 1 and rc["bottom"] <= r["vh"] + 1,
              "rect=%s vw=%d vh=%d" % ({k: round(v) for k, v in rc.items()}, r["vw"], r["vh"]))
        pg.screenshot(path=os.path.join(OUT, "ss_panel_open.png"))

    print()
    print("=" * 74)
    print("[5] 深色主题下面板不能是浅色斑")
    print("=" * 74)
    for theme, want_bg in (("light", None), ("dark", None)):
        pg.evaluate("t => localStorage.setItem('pms-theme', t)", theme)
        pg.goto(BASE + "/inventory/records")
        pg.wait_for_load_state("networkidle")
        r = pg.evaluate(PANEL, "material_id")
        if r.get("err"):
            check("%s 主题面板能打开" % theme, False, r["err"])
            continue
        bg = r["panelBg"]
        m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", bg)
        lum = None
        if m:
            f = lambda v: (v / 255) / 12.92 if (v / 255) <= 0.04045 else (((v / 255) + 0.055) / 1.055) ** 2.4
            rr, gg, bb = (int(m.group(i)) for i in (1, 2, 3))
            lum = 0.2126 * f(rr) + 0.7152 * f(gg) + 0.0722 * f(bb)
        # 浅色主题面板应该是白的（亮）；深色主题必须暗（<0.10，与 test_no_light_surfaces 同门槛）
        ok = (lum > 0.5) if theme == "light" else (lum is not None and lum < 0.10)
        check("%s 主题面板底色正确（%s，亮度 %.3f）" % (theme, bg, lum if lum is not None else -1), ok)
        if theme == "dark":
            pg.screenshot(path=os.path.join(OUT, "ss_panel_dark.png"))

    print()
    print("=" * 74)
    print("[6] 键盘：↓ 高亮、Enter 选中、Esc 关闭")
    print("=" * 74)
    pg.evaluate("t => localStorage.setItem('pms-theme', 'light')")
    pg.goto(BASE + "/inventory/records")
    pg.wait_for_load_state("networkidle")
    kb = pg.evaluate(r"""() => {
      const sel = document.querySelector('select[name=material_id]');
      const wrap = sel.closest('.ss');
      const btn = wrap.querySelector('.ss-toggle');
      btn.focus();
      const r = {};
      btn.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
      r.opened = !wrap.querySelector('.ss-panel').hidden;
      const search = wrap.querySelector('.ss-search');
      r.focusInSearch = document.activeElement === search;
      search.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
      r.hasActive = !!wrap.querySelector('.ss-opt.is-active');
      search.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
      const first = wrap.querySelector('.ss-opt.is-active');
      r.activeText = first ? first.textContent.trim().slice(0, 14) : '';
      search.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      r.closedByEsc = wrap.querySelector('.ss-panel').hidden;
      r.focusBackToBtn = document.activeElement === btn;
      r.ariaRole = search.getAttribute('role');
      r.ariaExpanded = search.getAttribute('aria-expanded');
      return r;
    }""")
    check("方向键能打开面板", kb["opened"])
    check("打开后焦点在搜索框（键盘用户能直接打字）", kb["focusInSearch"])
    check("↓ 能移动高亮（高亮了「%s」）" % kb["activeText"], kb["hasActive"])
    check("Esc 能关闭", kb["closedByEsc"])
    check("Esc 后焦点回到触发按钮（不是丢到 body 上）", kb["focusBackToBtn"])
    check("搜索框是 role=combobox（屏幕阅读器认得）", kb["ariaRole"] == "combobox",
          "role=%s expanded=%s" % (kb["ariaRole"], kb["ariaExpanded"]))

    print()
    print("=" * 74)
    print("[7] 筛选下拉选中后要真的提交（GET 表单自动跳转）")
    print("=" * 74)
    pg.goto(BASE + "/suppliers")
    pg.wait_for_load_state("networkidle")
    before = pg.url
    # ⚠ 必须**按按钮文字**挑菜单，不能用 querySelector('.ms-menu')：
    #   那是第一个（分类），不是「供应商」。第一版就这么写的，
    #   测的其实一直是分类下拉，报告里却写「供应商筛选」——
    #   标签与被测对象不符，测试通过时给人「供应商那边验过了」的错觉。
    picked = pg.evaluate(r"""() => {
      const m = [...document.querySelectorAll('.ms-menu')].find(
        x => x.parentElement.querySelector('button').textContent.includes('供应商'));
      if (!m) return { err: '没有「供应商」筛选菜单' };
      const s = m.querySelector('.ms-search');
      if (!s) return { err: '「供应商」筛选菜单没有搜索框' };
      const rows = [...m.querySelectorAll('.form-check:not(.ms-all)')];
      const before = rows.filter(r => !r.hidden).length;
      const word = (rows[0].textContent || '').trim().slice(0, 2);
      s.value = word; s.dispatchEvent(new Event('input', { bubbles: true }));
      const after = rows.filter(r => !r.hidden).length;
      // 长公司名不该被裁掉半个字（menu 宽度不够时 overflow 会切）
      const cut = rows.filter(r => !r.hidden).filter(x => {
        const l = x.querySelector('label');
        return l && l.scrollWidth > l.clientWidth + 1;
      }).length;
      return { before, after, word, cut };
    }""")
    if picked.get("err"):
        check("供应商筛选下拉有搜索框", False, picked["err"])
    else:
        check("供应商筛选下拉（%d 项）搜索生效：%d -> %d，关键词「%s」"
              % (picked["before"], picked["before"], picked["after"], picked["word"]),
              0 < picked["after"] < picked["before"])
        check("长公司名不被裁掉半截（长列表里最容易出问题的一处）",
              picked["cut"] == 0, "有 %d 条被裁" % picked["cut"])
    # 勾一个复选框，看 URL 有没有变。
    # ⚠ fltSubmit 走的是 fetch + history.replaceState（只换表格区不整页刷新），
    #   所以**不能**用 wait_for_load_state 等 —— 调用那一刻网络本来就是 idle 的，
    #   它会立刻返回，测出来的「URL 没变」是假的。
    #   必须轮询等 replaceState 发生。第一版就栽在这里。
    pg.goto(BASE + "/suppliers")
    pg.wait_for_load_state("networkidle")
    before = pg.url
    pg.evaluate(r"""() => {
      const cb = document.querySelector('.ms-menu .form-check:not(.ms-all) input[type=checkbox]');
      if (cb) { cb.checked = true; cb.dispatchEvent(new Event('change', { bubbles: true })); }
    }""")
    changed = False
    for _ in range(40):                      # 最多等 4 秒
        pg.wait_for_timeout(100)
        if pg.url != before:
            changed = True
            break
    check("勾选筛选后 URL 带上筛选参数（fetch + replaceState 路径）",
          changed, "%s -> %s" % (before.replace(BASE, ""), pg.url.replace(BASE, "")))

    print()
    print("=" * 74)
    print("[8] 整页 JS 没有报错（组件不能把别的功能带崩）")
    print("=" * 74)
    check("逐页无 pageerror", not page_errors,
          "; ".join("%s: %s" % (u, e[:60]) for u, e in list(page_errors.items())[:4]))

    br.close()

bad = [n for n, ok, _ in results if not ok]
print()
print("=" * 74)
if bad:
    print("!! %d 项失败（共 %d 项）" % (len(bad), len(results)))
    for n in bad:
        print("   -", n)
    sys.exit(1)
print("全部通过：%d 项" % len(results))
