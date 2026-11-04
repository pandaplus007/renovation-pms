"""新工作流界面的视觉留档：新状态列、审批留痕、执行关联、结算前检查。

只截图 + 几个关键元素的断言，不做业务逻辑校验（那是
test_change_workflow.py 的事）。目的是「肉眼能看出有没有排版塌掉」——
服务端全绿但列宽挤成一团的例子见过太多。
"""
from __future__ import annotations

import atexit
import os
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.database import SessionLocal  # noqa: E402
from app.models import ChangeOrder, Project  # noqa: E402

BASE = "http://127.0.0.1:8018"
OUT = os.path.join(ROOT, "tests", "shots")
os.makedirs(OUT, exist_ok=True)
MARK = "__ui测试 "

P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


db = SessionLocal()
pid = db.query(Project).order_by(Project.id).first().id
for old in db.query(ChangeOrder).filter(ChangeOrder.title.like(MARK)).all():
    db.delete(old)
db.commit()

a = ChangeOrder(project_id=pid, title=MARK + "主卧衣帽间加装", category="木工",
                amount=28600, cost=19400, status="已定价待业主审",
                change_no="UI-018", reason="业主要求增加收纳",
                pricing_note="按实际木作用量计价", submitted_by="admin")
b = ChangeOrder(project_id=pid, title=MARK + "厨房取消吊顶", category="水电",
                amount=-6800, cost=3100, status="已驳回", change_no="UI-017",
                reject_reason="业主自行协调，不计入合同")
c = ChangeOrder(project_id=pid, title=MARK + "卫生间防水重做", category="泥瓦",
                amount=9400, cost=7600, status="已确认", change_no="UI-016",
                signer="王工", approved_by="王工")
for o in (a, b, c):
    db.add(o)
db.commit()
CID = {"a": a.id, "b": b.id, "c": c.id}
db.close()


@atexit.register
def _cleanup():
    """必须用 atexit —— 中途 Playwright 超时也要清。

    踩过的坑：like(MARK) 里**必须带 %**。MARK 结尾只有空格时
    LIKE 变成「整串精确匹配」，删不掉任何东西还不报错——夹具就永久留库了。
    清理失败是静默的，所以这条格外要紧。
    """
    d = SessionLocal()
    n = d.query(ChangeOrder).filter(ChangeOrder.title.like(MARK + "%")).delete(
        synchronize_session=False)
    d.commit()
    if n:
        print("已清理临时夹具 %d 条" % n)
    d.close()


with sync_playwright() as pw:
    br = pw.chromium.launch()
    pg = br.new_page(viewport={"width": 1560, "height": 1000})
    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    # ---- 1. 项目变更页：状态列 + 驳回理由 + 流转按钮 ----
    pg.goto("%s/projects/%s/changes" % (BASE, pid))
    pg.wait_for_load_state("networkidle")
    pg.screenshot(path=os.path.join(OUT, "wf_01_project.png"), full_page=True)

    body = pg.inner_text("body")
    check("新状态出现在状态列", "已定价待业主审" in body)
    check("待签状态标「可催签」", "可催签" in body)
    check("驳回理由直接显示在列表里", "业主自行协调" in body)
    check("有结算前检查按钮（clipboard-check）",
          pg.locator("a[href='/changes/%d/settle-check']" % CID["c"]).count() == 1)
    # 驳回按钮不能被包在 form 里（HTML 禁止嵌套 form，浏览器会整块丢弃）
    check("驳回按钮不是 form 包裹",
          pg.locator("form button.js-reject").count() == 0
          and pg.locator("button.js-reject").count() >= 1)

    # 表格没有横向溢出到看不见列
    ov = pg.evaluate("""() => {
      const t = document.querySelectorAll('.table-responsive')[1]
                || document.querySelector('.table-responsive');
      return t ? t.scrollWidth - t.clientWidth : -1;
    }""")
    check("明细表横向滚动量合理（<200px 即未溢出）", -1 <= ov < 200, ov)

    # ---- 2. 编辑页：审批留痕 + 执行关联 ----
    pg.goto("%s/changes/%d/edit" % (BASE, CID["c"]))
    pg.wait_for_load_state("networkidle")
    pg.screenshot(path=os.path.join(OUT, "wf_02_edit.png"), full_page=True)
    body = pg.inner_text("body")
    check("编辑页有「定价依据」", "定价依据" in body)
    check("编辑页有「执行关联」", "执行关联" in body)
    check("已确认态给结算前检查入口",
          pg.locator("a[href='/changes/%d/settle-check']" % CID["c"]).count() >= 1)
    # 关联选择器不能把最后一行切掉半截——「哪几张单还没付清」正是
    # 要看的东西，露半行等于藏了一个可选项，而服务端完全测不出来。
    #
    # 判据不是「有没有溢出」：size=6 而选项 60 条，溢出是**应该**的
    # （要能往下滚）。真正的问题是框高是不是行高的整倍数——
    # 不是整倍数就说明底部压着半行。
    clip = pg.eval_on_selector_all(
        "select.link-picker",
        """els => els.filter(e => e.options.length > 0).map(e => {
             const probe = document.createElement('option');
             probe.textContent = '测';
             e.appendChild(probe);
             const row = probe.getBoundingClientRect().height;
             e.removeChild(probe);
             if (!row) return -1;
             return Math.abs(e.clientHeight / row -
                             Math.round(e.clientHeight / row)) * row;
           })""")
    check("关联选择器框高是整行（底部没切半行）",
          all(0 <= d < 2 for d in clip), clip)
    check("关联选择器没套 Bootstrap 的 form-select（会裁行）",
          pg.locator("select.link-picker.form-select").count() == 0)
    # 驳回理由默认隐藏，状态切到已驳回才出现
    check("驳回理由默认隐藏（非驳回态）",
          pg.locator("#rejectWrap[hidden]").count() == 1)
    pg.select_option("select[name=status]", "已驳回")
    pg.wait_for_timeout(200)
    check("切到已驳回后理由字段出现",
          pg.locator("#rejectWrap[hidden]").count() == 0)
    check("切到已驳回后理由为必填",
          pg.eval_on_selector("#rejectWrap input[name=reject_reason]",
                              "el => el.required") is True)

    # ---- 3. 结算前检查页 ----
    pg.goto("%s/changes/%d/settle-check" % (BASE, CID["c"]))
    pg.wait_for_load_state("networkidle")
    pg.screenshot(path=os.path.join(OUT, "wf_03_settle.png"), full_page=True)
    body = pg.inner_text("body")
    for lbl in ("关联执行单据", "对应成本已录入", "签字扫描件已归档"):
        check("检查页列出「%s」" % lbl, lbl in body)
    check("检查页有强制放行按钮", "强制标记为已结算" in body)
    check("检查页说明每项在查什么", "这些检查在查什么" in body)

    # ---- 4. 总览页：待签字单列 ----
    pg.goto(BASE + "/changes")
    pg.wait_for_load_state("networkidle")
    pg.screenshot(path=os.path.join(OUT, "wf_04_list.png"), full_page=True)
    body = pg.inner_text("body")
    check("总览有「待签字」口径", "待签字" in body)
    check("总览提示已报业主的部分", "已报业主" in body)

    # ---- 5. 待办总览 ----
    pg.goto(BASE + "/todo")
    pg.wait_for_load_state("networkidle")
    pg.screenshot(path=os.path.join(OUT, "wf_05_todo.png"), full_page=True)
    body = pg.inner_text("body")
    check("待办列出「变更待业主签字」", "变更待业主签字" in body)
    check("待办列出「变更缺签字扫描件」", "变更缺签字扫描件" in body)

    br.close()

print()
if F:
    print("失败 %d 项：%s" % (len(F), "、".join(F)))
    sys.exit(1)
print("通过 %d 项，失败 0" % len(P))
print("截图在 %s" % OUT)