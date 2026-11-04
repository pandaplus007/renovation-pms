"""浏览器里验证「阶段未开始」提示的实际呈现。

要确认三件事：
  1. 该提示的项目（#1）真的挂出来了
  2. 不该提示的项目（#2 待签约）确实没有——误报比漏报更伤
  3. 提示用的是琥珀色而不是红色，且在打印时能出纸（它是数据问题，
     纸质单据上也该看得见；进度条那种装饰才该隐藏）
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.database import SessionLocal  # noqa: E402
from app.models import Project  # noqa: E402

BASE = "http://127.0.0.1:8018"
OUT = os.path.join(ROOT, "tests", "shots")
os.makedirs(OUT, exist_ok=True)

db = SessionLocal()
expect_flag = {p.id: p.phase_never_started
               for p in db.query(Project).order_by(Project.id).all()}
db.close()
print("期望（模型判定）：%s" % expect_flag)

P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


def flags_on(pg):
    """返回每行的：项目名 + 该行是否有「阶段未开始」提示。"""
    return pg.evaluate("""() => {
      const out = [];
      document.querySelectorAll('table tbody tr').forEach(tr => {
        const link = tr.querySelector('td a');
        if (!link) return;
        out.push({
          name: link.textContent.trim(),
          flag: [...tr.querySelectorAll('.badge')]
                  .some(b => /阶段未开始/.test(b.textContent)),
        });
      });
      return out;
    }""")


with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_context(viewport={"width": 1400, "height": 950}).new_page()
    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    print()
    print("=" * 76)
    print("[1] 项目列表：提示挂在正确的行上")
    print("=" * 76)
    pg.goto(BASE + "/projects")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(400)
    check("无渲染错误", "Traceback" not in pg.inner_text("body"))
    rows = flags_on(pg)
    for r in rows:
        print("    %-30s 提示=%s" % (r["name"][:28], r["flag"]))
    n_flag = sum(1 for r in rows if r["flag"])
    n_expect = sum(1 for v in expect_flag.values() if v)
    check("提示条数与模型判定一致（%d vs 期望 %d）" % (n_flag, n_expect),
          n_flag == n_expect, (n_flag, n_expect))
    # 待签约那个项目不该有
    check("待签约项目没有提示（误报比漏报更伤）",
          not any(r["flag"] and "写字楼" in r["name"] for r in rows), rows)

    print()
    print("=" * 76)
    print("[2] 提示用的是琥珀色，不是红色")
    print("=" * 76)
    colors = pg.evaluate("""() => {
      const el = [...document.querySelectorAll('.badge')]
        .find(b => /阶段未开始/.test(b.textContent));
      if (!el) return null;
      const cs = getComputedStyle(el);
      return {bg: cs.backgroundColor, fg: cs.color,
              cls: el.className, title: el.title};
    }""")
    print("    %s" % colors)
    check("提示元素存在", colors is not None)
    if colors:
        check("用 state-watch 档", "state-watch" in colors["cls"], colors)
        check("不是 state-alert", "state-alert" not in colors["cls"], colors)
        check("背景不透明（有实际底色）",
              not colors["bg"].startswith("rgba(0, 0, 0, 0)"), colors)
        check("有 title 说明为什么", "未开始" in (colors["title"] or ""), colors)
    pg.screenshot(path=os.path.join(OUT, "phase_flag_list.png"))

    print()
    print("=" * 76)
    print("[3] dashboard 也挂，且与列表一致")
    print("=" * 76)
    pg.goto(BASE + "/")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    check("dashboard 无渲染错误", "Traceback" not in pg.inner_text("body"))
    drows = flags_on(pg)
    for r in drows:
        print("    %-30s 提示=%s" % (r["name"][:28], r["flag"]))
    check("dashboard 提示条数与列表一致（%d）" % len(drows),
          sum(1 for r in drows if r["flag"]) == n_flag, drows)
    pg.screenshot(path=os.path.join(OUT, "phase_flag_dash.png"))

    print()
    print("=" * 76)
    print("[4] 项目详情：提示 + 解释文案")
    print("=" * 76)
    target = [i for i, v in expect_flag.items() if v]
    if target:
        pg.goto(BASE + "/projects/%d" % target[0])
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(300)
        txt = pg.inner_text("body")
        check("详情页无渲染错误", "Traceback" not in txt)
        check("详情页显示「阶段未开始，请核实」", "阶段未开始，请核实" in txt,
              [x for x in txt.split("\n") if "阶段" in x][:4])
        check("给出解释「两个数据源必有一个不对」",
              "两个数据源必有一个不对" in txt,
              [x for x in txt.split("\n") if "数据源" in x][:2])
        pg.screenshot(path=os.path.join(OUT, "phase_flag_detail.png"))

    print()
    print("=" * 76)
    print("[5] 打印时提示要出纸（数据问题该看得见，与装饰性进度条不同）")
    print("=" * 76)
    if target:
        pg.goto(BASE + "/projects/%d" % target[0])
        pg.wait_for_load_state("networkidle")
        pg.emulate_media(media="print")
        pg.wait_for_timeout(300)
        vis = pg.evaluate("""() => {
          const el = [...document.querySelectorAll('.badge')]
            .find(b => /阶段未开始/.test(b.textContent));
          if (!el) return null;
          const cs = getComputedStyle(el);
          return {display: cs.display, visibility: cs.visibility,
                  w: el.getBoundingClientRect().width};
        }""")
        print("    %s" % vis)
        check("打印时提示仍然可见", vis and vis["display"] != "none"
              and vis["w"] > 0, vis)
        pg.pdf(path=os.path.join(OUT, "phase_flag_print.pdf"), format="A4")
        pg.emulate_media(media="screen")

    print()
    print("=" * 76)
    print("[6] 移动端不溢出")
    print("=" * 76)
    pg2 = b.new_context(viewport={"width": 390, "height": 844}).new_page()
    pg2.goto(BASE + "/login")
    pg2.fill("input[name=username]", "admin")
    pg2.fill("input[name=password]", "admin123")
    pg2.click("button[type=submit]")
    pg2.wait_for_load_state("networkidle")
    for path in ["/projects", "/"]:
        pg2.goto(BASE + path)
        pg2.wait_for_load_state("networkidle")
        pg2.wait_for_timeout(300)
        ov = pg2.evaluate("""() => ({
          doc: document.documentElement.scrollWidth,
          win: window.innerWidth,
        })""")
        check("%s 无横向溢出（%d <= %d）" % (path, ov["doc"], ov["win"]),
              ov["doc"] <= ov["win"] + 1, ov)
    pg2.screenshot(path=os.path.join(OUT, "phase_flag_mobile.png"))

    b.close()

print()
print("=" * 76)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
print("截图：", OUT)
print("=" * 76)
sys.exit(1 if F else 0)