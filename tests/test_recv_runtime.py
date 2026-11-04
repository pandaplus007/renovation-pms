"""浏览器里验证两个进度：数字对不对、有没有把「无数据」显示成 0%。

自动检查过了属性层，但没验证「页面上真的分成了两列、数字对得上账本」。
这里读 DOM 文本，和数据库手算值比对。
"""
from __future__ import annotations

import os
import re
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
expect = {}
for p in db.query(Project).order_by(Project.id).all():
    expect[p.name] = {
        "id": p.id,
        "pct": None if p.recv_pct is None else round(p.recv_pct),
        "amt": p.recv_amount,
        "phases": len(p.phases),
    }
db.close()
print("期望值（来自数据库手算）：")
for k, v in expect.items():
    print("  #%d %-26s 回款=%s 已收=%.0f 阶段数=%d"
          % (v["id"], k[:24], ("%d%%" % v["pct"]) if v["pct"] is not None else "—",
             v["amt"], v["phases"]))

P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


def page_text(pg):
    return pg.evaluate("() => document.body.innerText")


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
    print("[1] dashboard：列头是「回款」，且显示 67%")
    print("=" * 76)
    pg.goto(BASE + "/")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    txt = page_text(pg)
    check("无渲染错误", "Traceback" not in txt)
    check("列头为「回款」", "回款" in txt, txt[:200])
    # 只查表格列头，不查整页文字：收款提醒里「第二笔进度款」是业务说法，
    # 不是列头。整页搜「进度」会把正常文案一起判成违规。
    heads = pg.eval_on_selector_all(
        "table thead th", "els => els.map(e => e.innerText.trim())")
    print("  最近项目表头：%s" % heads)
    check("列头不含含混的「进度」", "进度" not in heads, heads)
    check("列头含「回款」", "回款" in heads, heads)
    for name, v in expect.items():
        if v["pct"] is not None:
            check("dashboard 显示 %d%%（%s）" % (v["pct"], name[:14]),
                  "%d%%" % v["pct"] in txt, "%d%%" % v["pct"])
    pg.screenshot(path=os.path.join(OUT, "recv_dashboard.png"), full_page=False)

    print()
    print("=" * 76)
    print("[2] 项目列表：两列并存，表头分别是「施工」「回款」")
    print("=" * 76)
    pg.goto(BASE + "/projects")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    heads = pg.eval_on_selector_all(
        "table thead th", "els => els.map(e => e.innerText.trim())")
    print("  表头：%s" % heads)
    check("表头含「回款」", "回款" in heads, heads)
    check("表头含「施工」", "施工" in heads, heads)
    check("表头不含含混的「进度」", "进度" not in heads, heads)
    txt = page_text(pg)
    for name, v in expect.items():
        if v["pct"] is not None:
            check("列表显示 %d%%（%s）" % (v["pct"], name[:14]),
                  "%d%%" % v["pct"] in txt, "%d%%" % v["pct"])
    # 无阶段的项目必须显示「未建阶段」而不是 0%
    nophase = [v for v in expect.values() if v["phases"] == 0]
    if nophase:
        check("无阶段项目显示「未建阶段」而非 0%",
              "未建阶段" in txt, [x for x in txt.split("\n") if "阶段" in x][:3])
    pg.screenshot(path=os.path.join(OUT, "recv_projects.png"), full_page=False)

    print()
    print("=" * 76)
    print("[3] 项目详情：头部两个进度并列，各带金额/阶段说明")
    print("=" * 76)
    pg.goto(BASE + "/projects/1")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    txt = page_text(pg)
    check("无渲染错误", "Traceback" not in txt)
    check("有「回款」标签", "回款" in txt)
    check("有「施工」标签", "施工" in txt)
    v = expect[[k for k in expect if expect[k]["id"] == 1][0]]
    check("显示 %d%%" % v["pct"], "%d%%" % v["pct"] in txt)
    check("显示「已收 ¥…」金额说明", "已收 ¥" in txt,
          [x for x in txt.split("\n") if "已收" in x][:2])
    # 阶段说明按实际情况分支：项目其实有 7 个阶段（之前误以为没有），
    # 所以显示的是「7 个阶段，0 个已完成」而不是「未录入施工阶段」。
    v = expect[[k for k in expect if expect[k]["id"] == 1][0]]
    if v["phases"] == 0:
        check("无阶段项目显示「未录入施工阶段」", "未录入施工阶段" in txt,
              [x for x in txt.split("\n") if "阶段" in x][:3])
    else:
        check("有阶段时显示阶段数与完成数（%d 个阶段）" % v["phases"],
              ("%d 个阶段" % v["phases"]) in txt,
              [x for x in txt.split("\n") if "阶段" in x][:3])
        check("不出现「未录入施工阶段」（阶段是有的）",
              "未录入施工阶段" not in txt)
    pg.screenshot(path=os.path.join(OUT, "recv_detail.png"), full_page=False)

    print()
    print("=" * 76)
    print("[4] 客户详情：项目表格也分成两列")
    print("=" * 76)
    pg.goto(BASE + "/customers/1")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    heads = pg.eval_on_selector_all(
        "table thead th", "els => els.map(e => e.innerText.trim())")
    print("  表头：%s" % heads)
    check("表头含「施工」与「回款」",
          "施工" in heads and "回款" in heads, heads)
    check("无渲染错误", "Traceback" not in page_text(pg))
    pg.screenshot(path=os.path.join(OUT, "recv_customer.png"), full_page=False)

    print()
    print("=" * 76)
    print("[5] 打印视图：进度条按设计隐藏，但百分比数字必须出纸")
    print("=" * 76)
    pg.goto(BASE + "/projects/1")
    pg.wait_for_load_state("networkidle")
    # 打印样式里 .progress 是 display:none —— 条形是装饰，印出来是灰块。
    # 真正要出现在纸面上的是旁边的百分比数字（它在 .progress 之外）。
    # 所以这里验的是数字，不是条。之前反过来验条形，判失败是测试写错目标。
    pg.emulate_media(media="print")
    pg.wait_for_timeout(300)
    bars = pg.evaluate("""() => {
      const b = [...document.querySelectorAll('.progress-bar')];
      return {total: b.length,
              visible: b.filter(x => x.getBoundingClientRect().width > 0).length};
    }""")
    print("  进度条：共 %d 个，打印时可见 %d 个（预期 0，按设计隐藏）"
          % (bars["total"], bars["visible"]))
    check("进度条在打印时全部隐藏", bars["visible"] == 0, bars)

    nums = pg.evaluate("""() => {
      const want = ['回款', '施工'];
      const out = {};
      want.forEach(w => {
        const el = [...document.querySelectorAll('.small.fw-semibold')]
          .find(e => e.innerText.trim() === w);
        const box = el && el.parentElement;
        out[w] = box ? box.innerText.replace(/\\s+/g, ' ').trim().slice(0, 60) : null;
      });
      return out;
    }""")
    for k, val in nums.items():
        print("  %s 区块：%s" % (k, val))
    check("打印时「回款」区块有文字", bool(nums.get("回款")), nums)
    check("打印时「施工」区块有文字", bool(nums.get("施工")), nums)
    check("打印时回款百分比出纸", "%" in (nums.get("回款") or ""), nums.get("回款"))

    pg.pdf(path=os.path.join(OUT, "recv_print.pdf"), format="A4")
    pg.emulate_media(media="screen")

    b.close()

print()
print("=" * 76)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
print("截图与 PDF：", OUT)
print("=" * 76)
sys.exit(1 if F else 0)