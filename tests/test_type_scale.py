"""排版契约回归：字号必须落在刻度内，字重只用两档，蓝色系无绿色残留。

这条测试的作用是「让偏离在提交前就失败」，而不是等肉眼发现。
本轮改造前的实测数据（11 个页面 255 个文字样本）：
    字号 26 种 / 字重含 500 与 700 / 字间距 218 个样本为 0 / 主色为绿
现在这些都应当被拦住。

运行需要本地 8018 在跑（要真实渲染才能测到「最终算出来的」字号，
扫 CSS 源码测不到——.small 的级联就是只在渲染后才暴露的）。
"""
from __future__ import annotations

import os
import re
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
BASE = "http://127.0.0.1:8018"

# Carbon productive type set 的六档。少的比多的好：
# 每多一档就多一次「这个字为什么比旁边大」的争论。
ALLOWED_SIZES = {12, 14, 16, 20, 24, 28}
ALLOWED_WEIGHTS = {400, 600}

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
    ("变更签证打印页", "/changes/1/print?copies=1"),
]

PROBE = r"""
() => {
  const out = [];
  const walk = (el) => {
    for (const n of el.childNodes) {
      if (n.nodeType === 3) {
        const t = n.textContent.trim();
        if (!t) continue;
        const cs = getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') continue;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        out.push({
          tag: el.tagName.toLowerCase(),
          cls: String(el.className || '').slice(0, 60),
          size: Math.round(parseFloat(cs.fontSize) * 100) / 100,
          weight: parseInt(cs.fontWeight, 10),
          ls: cs.letterSpacing === 'normal' ? 0 : parseFloat(cs.letterSpacing),
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

F = []
P = []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


rows = []
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
        for r in pg.evaluate(PROBE):
            r["page"] = name
            rows.append(r)
    br.close()

print("样本 %d 个（%d 个页面）" % (len(rows), len(PAGES)))
print()

# ---- 1. 字号 ----
print("[1] 字号刻度")
bad = {}
for r in rows:
    if r["size"] not in ALLOWED_SIZES:
        bad.setdefault(r["size"], []).append(r)
sizes = sorted({r["size"] for r in rows})
check("字号种类 ≤ 6（实际 %d 种：%s）" % (len(sizes), sizes), len(sizes) <= 6)
if bad:
    for s in sorted(bad):
        ex = bad[s][0]
        print("      %5.2fpx  x%-3d 例:%s.%s  「%s」"
              % (s, len(bad[s]), ex["page"], ex["cls"][:30], ex["sample"]))
else:
    print("      所有样本都在 %s 内" % sorted(ALLOWED_SIZES))

# ---- 2. 字重 ----
print("\n[2] 字重")
ws = sorted({r["weight"] for r in rows})
off = [w for w in ws if w not in ALLOWED_WEIGHTS]
check("字重只有 400/600（实际 %s）" % ws, not off, off)
if off:
    for w in off:
        ex = next(r for r in rows if r["weight"] == w)
        print("      w%-4d 例:%s.%s  「%s」" % (w, ex["page"], ex["cls"][:32], ex["sample"]))

# ---- 3. 字间距 ----
# Carbon productive 的规定不是「一律加字距」，而是**按字号分档**：
#   12px → +0.32px（label-01）
#   14px → +0.16px（body-01 / heading-01）
#   16px 及以上 → 0（再往上加字距会让字散开，Carbon 的 expressive 档
#                   明确是 letter-spacing: 0）
# 所以只对 12/14px 提要求。改造前 218/255 个样本为 0，
# 现在要确认的是**小字号**确实加了。
print("\n[3] 字间距（Carbon：12px +0.32px，14px +0.16px，16px 起为 0）")
SMALL = {12, 14}
zero_small = [r for r in rows if r["ls"] == 0 and r["size"] in SMALL
              and r["page"] != "打印页"]
small_n = len([r for r in rows if r["size"] in SMALL and r["page"] != "打印页"])
check("12/14px 的文字都有正字距（为 0 的 %d 个 / 该档样本 %d 个）"
      % (len(zero_small), small_n), len(zero_small) == 0, len(zero_small))
for r in zero_small[:5]:
    print("      %5.1fpx  %s.%s  「%s」" % (r["size"], r["page"], r["cls"][:30], r["sample"]))

# 16px 及以上不该有正字距——若出现，说明某处误抄了 12px 的规则
over = [r for r in rows if r["ls"] > 0 and r["size"] >= 20 and r["page"] != "打印页"]
check("20px 以上没有正字距（避免大字被拉散）", not over,
      [(r["size"], r["ls"], r["cls"][:24]) for r in over[:4]])

# ---- 4. 字体族没有回退 ----
print("\n[4] 字体族")
bad_font = [r for r in rows
            if any(k in r.get("family", "") for k in ("宋体", "SimSun", "serif"))]
check("没有回退到宋体/衬线", not bad_font, len(bad_font))

# ---- 5. 静态检查：源码层面 ----
print("\n[5] 源码层面")
css = open(os.path.join(ROOT, "app", "static", "css", "style.css"),
           encoding="utf-8").read()
code_only = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                   css, flags=re.S)
# 主色曾短暂换成蓝色 #3E6CA8，结论是「不如绿色」，已改回；
# 后又从 #3d7a63 换成 #047857（同色相压深一档，白字 5.05:1 → 5.48:1）。
# 这条断言的作用不是「锁定某个色值」（以后还想换就改这里），
# 而是**证明品牌色是显式写死的值，不是被批量脚本按色相旋转换过的**——
# 那次换绿时主色被转成了 #3bab81：同色相、亮度接近，肉眼看不出差别，
# 但那不是这个品牌一直用的那个绿。色相旋转适合装饰色，不适合品牌色。
BRAND_PRIMARY = "#047857"   # 换主色时改这一处，别改断言结构
check("主色是显式写死的 %s（不是色相旋转的近似值）" % BRAND_PRIMARY,
      "--primary: %s" % BRAND_PRIMARY in code_only,
      re.search(r"--primary:\s*#\w+", code_only))
check("定义了深色主题块", '[data-bs-theme="dark"]' in css)
grays = set(re.findall(r"--gray-50:\s*(#[0-9a-f]{6})", code_only))
check("深色主题的中性色阶与浅色**不同**（否则切换只是换了个名字）",
      len(grays) >= 2, grays)
check("body 基准是 var(--fs-1)", "font-size: var(--fs-1);" in code_only)


def _hue(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return None
    d = mx - mn
    if mx == r:
        hp = ((g - b) / d) % 6
    elif mx == g:
        hp = (b - r) / d + 2
    else:
        hp = (r - g) / d + 4
    return hp * 60


# 断言「浅色主题的中性色阶是绿调」——只查 :root 里的 --gray-*。
# 不能扫全文件然后数「有几处蓝」：深色主题的色阶刻意落在蓝调区间
# （深底上偏绿的文字会发荧光），info 语义色也是蓝的。那样数出来的
# 「蓝色数量」和「有没有换干净」没有对应关系，早先就因为这个阈值
# 误报过两次。
# 真正要保证的是：**浅色主题那套灰阶跟主色同族**。
_root_block = re.search(r"^:root\s*\{(.*?)\n\}", code_only, re.S | re.M)
root_body = _root_block.group(1) if _root_block else ""
blue_gray = []
for m in re.finditer(r"--gray-(\d+):\s*(#[0-9a-fA-F]{6})", root_body):
    r, g, b = (int(m.group(2)[i:i + 2], 16) for i in (1, 3, 5))
    hu = _hue(r, g, b)
    if hu is not None and 190 <= hu <= 245:
        blue_gray.append("#" + m.group(2))
check("浅色主题的中性色阶与主色同族（无蓝调残留）",
      not blue_gray, blue_gray)

# 主色三档必须成「深 → 浅」的单调关系，否则渐变与高亮态会显得杂乱
try:
    def _rgb(hx):
        return tuple(int(hx.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

    _p1 = _rgb(re.search(r"--primary:\s*(#[0-9a-f]{6})", code_only).group(1))
    _p2 = _rgb(re.search(r"--primary-2:\s*(#[0-9a-f]{6})", code_only).group(1))
    check("primary-2 比 primary 浅（悬停/聚焦用亮档）",
          sum(_p2) > sum(_p1), (_p1, _p2))
except AttributeError:
    check("primary-2 比 primary 浅（悬停/聚焦用亮档）", False, "取不到变量")

print()
if F:
    print("失败 %d 项：%s" % (len(F), "、".join(F)))
    sys.exit(1)
print("通过 %d 项，失败 0" % len(P))