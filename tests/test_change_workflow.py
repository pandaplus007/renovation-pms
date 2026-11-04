# -*- coding: utf-8 -*-
"""变更签证工作流回归：新增状态、驳回理由、执行关联、结算前检查。

覆盖本轮加的四项：
  1.「已定价待业主审」状态存在，且**不计入结算金额**
  2. 驳回理由单列成字段，驳回时必填
  3. 转「已结算」有前置检查，缺项提示后可强制放行
  4. 状态机不许倒退

这些是「账面数字」的守卫，不只是界面检查——一旦破了，
项目结算金额和毛利会悄悄错掉，而且没人会发现。
"""
import atexit
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from app.changes import (AWAITING_SIGN_STATUS, COUNTED_STATUS,
                         PENDING_STATUS, can_transition, settle_readiness)
from app.database import SessionLocal
from app.models import CHANGE_STATUS, ChangeOrder, Project

BASE = "http://127.0.0.1:8018"
FAILED = []


def check(name, cond, extra=""):
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, extra))
        FAILED.append(name)


# ---------------- 测试夹具 ----------------
db = SessionLocal()
_pid = db.query(Project).order_by(Project.id).first().id
_co = ChangeOrder(project_id=_pid, title="__wf测试 变更", category="水电",
                  amount=12345.0, cost=3000.0, status="草稿",
                  change_no="WF-TEST", reason="回归测试用")
db.add(_co)
db.commit()
CID = _co.id


@atexit.register
def _cleanup():
    """必须用 atexit —— 中途失败/超时也要清，否则测试单会永久留库。

    （早先写成立末尾裸语句，有一轮 Playwright 超时直接跳过了清理，
     留下脏数据让 test_newfeat 莫名失败。）
    """
    d = SessionLocal()
    d.query(ChangeOrder).filter(ChangeOrder.title.like("__wf测试%")).delete(
        synchronize_session=False)
    d.query(ChangeOrder).filter(ChangeOrder.change_no == "WF-T2").delete(
        synchronize_session=False)
    d.query(ChangeOrder).filter(ChangeOrder.title.like("__todo%")).delete(
        synchronize_session=False)
    d.commit()
    d.close()


s = requests.Session()
s.post(BASE + "/login", data={"username": "admin", "password": "admin123"})


def transition(cid, to, force="", reason=""):
    r = s.post("%s/changes/%s/transition" % (BASE, cid),
               data={"to": to, "force": force, "reject_reason": reason},
               allow_redirects=False)
    return r.status_code, r.headers.get("Location", "")


def status_now(cid):
    d = SessionLocal()
    o = d.get(ChangeOrder, cid)
    v = o.status if o else None
    d.close()
    return v


def _new_record(title):
    """再建一张测试单。用多张单而不是复用同一张：状态机是单向的，
    上一段测完的终态单没法拿来测下一个环节。"""
    d = SessionLocal()
    o = ChangeOrder(project_id=_pid, title=title, category="水电",
                    amount=8000.0, cost=2000.0, status="草稿", change_no="WF-T2")
    d.add(o)
    d.commit()
    i = o.id
    d.close()
    return i


def _edit_page(cid):
    """编辑页 HTML，剥掉 Jinja 注释。
    注释里写着「为什么这么改」，留着会让「页面出现某词」类断言
    变成永远真——必须剥。"""
    r = s.get("%s/changes/%s/edit" % (BASE, cid))
    return re.sub(r"\{#.*?#\}", "", r.text, flags=re.S)


print("=== 1. 新状态与记账口径 ===")
check("CHANGE_STATUS 含「已定价待业主审」",
      "已定价待业主审" in CHANGE_STATUS)
check("新状态**不**计入结算金额",
      "已定价待业主审" not in COUNTED_STATUS,
      "COUNTED_STATUS=%s" % (COUNTED_STATUS,))
check("新状态属待签", "已定价待业主审" in PENDING_STATUS)
check("新状态可单独识别（催签用）",
      AWAITING_SIGN_STATUS == ("已定价待业主审",))

print()
print("=== 2. 状态机方向性 ===")
check("草稿 → 已定价待业主审", can_transition("草稿", "已定价待业主审"))
check("已定价待业主审 → 已确认", can_transition("已定价待业主审", "已确认"))
check("已确认 → 已结算", can_transition("已确认", "已结算"))
check("已定价待业主审 → 已结算 被禁（须先确认）",
      not can_transition("已定价待业主审", "已结算"))
check("已结算是终态", not can_transition("已结算", "草稿"))
check("同状态不算流转", not can_transition("草稿", "草稿"))

print()
print("=== 3. 结算前检查（提示但不硬拦） ===")
d = SessionLocal()
_o = d.get(ChangeOrder, CID)
_o.status = "已确认"
d.commit()
chk = settle_readiness(d, _o)
d.close()
labels = {i["label"]: i["ok"] for i in chk["items"]}
check("没关联单据 → 不通过", chk["ready"] is False)
check("检查项含「关联执行单据」", "关联执行单据" in labels)
check("检查项含「对应成本已录入」", "对应成本已录入" in labels)
check("检查项含「签字扫描件已归档」", "签字扫描件已归档" in labels)
check("缺项都有可读说明",
      all(i["detail"] for i in chk["items"] if not i["ok"]))

code, loc = transition(CID, "已确认", force="")   # 已确认 → 已确认 非法
check("重复状态被拒并提示可选下一步",
      code == 303 and "/edit?err=" in loc, loc[:80])

code, loc = transition(CID, "已结算")
check("缺项转结算 → 挡到检查页而非放行",
      code == 303 and "/settle-check" in loc, loc[:80])
check("挡下后状态未变", status_now(CID) == "已确认")

r = s.get("%s/changes/%s/settle-check" % (BASE, CID))
page = r.text
check("检查页可访问", r.status_code == 200)
check("检查页逐项列出未满足", page.count("未满足") >= 2)
check("检查页给强制放行按钮", "强制标记为已结算" in page)

code, loc = transition(CID, "已结算", force="1")
check("force=1 放行", code == 303 and status_now(CID) == "已结算")
check("强制放行写了结算人",
      bool(SessionLocal().get(ChangeOrder, CID).settled_by))

print()
print("=== 4. 驳回理由单列成字段 ===")
# 用另一张单测驳回：CID 已经 force 到「已结算」，那是终态，
# 从它驳回本就该被拒（这条本身是对的，别用一张终态单来测驳回）。
RID = _new_record("__wf测试 驳回")
code, loc = transition(RID, "已驳回")
check("驳回不填理由被拒", code == 303 and "/edit?err=" in loc, loc[:80])

code, loc = transition(RID, "已驳回", reason="业主要求自行采购")
check("填理由后驳回成功", code == 303 and status_now(RID) == "已驳回")

d = SessionLocal()
o = d.get(ChangeOrder, RID)
reason = o.reject_reason
note = o.note
d.close()
check("理由落库（单列字段，不是拼进 note）", reason == "业主要求自行采购", repr(reason))
check("没被塞进备注字段", "自行采购" not in (note or ""))

r = s.get("%s/projects/%s/changes" % (BASE, _pid))
check("列表里能直接看到驳回理由", "业主要求自行采购" in r.text)

code, loc = transition(RID, "草稿")
check("已驳回可退回拟稿", status_now(RID) == "草稿")
d = SessionLocal()
o = d.get(ChangeOrder, RID)
check("不再是驳回态时理由被清空（不留旧结论）", not o.reject_reason, repr(o.reject_reason))
d.close()

print()
print("=== 5. 终态不再能流转 ===")
code, loc = transition(CID, "已驳回", reason="x")
check("已结算是终态，驳回也被拒",
      code == 303 and "/edit?err=" in loc and status_now(CID) == "已结算")

print()
print("=== 6. 界面元素在位 ===")
# 结算前检查入口只在「已确认」时出现（别的状态转不了结算，
# 提前显示一个走不通的按钮反而误导）。要用一张**确实是已确认**的
# 单来验，不能拿上一段 force 结算完的终态单——那张是「已结算」。
CID2 = _new_record("__wf测试 已确认")
transition(CID2, "已定价待业主审")
transition(CID2, "已确认")
check("准备一张已确认的单", status_now(CID2) == "已确认")
check("非已确认时不给结算检查入口",
      "settle-check" not in _edit_page(RID))
check("已确认时给结算检查入口", "settle-check" in _edit_page(CID2))
check("已结算是终态，也不再给结算检查入口",
      "settle-check" not in _edit_page(CID))

page = _edit_page(RID)
check("编辑页有「定价依据」", "pricing_note" in page)
check("编辑页有「驳回理由」输入框", "reject_reason" in page)
check("编辑页有「执行关联」", "执行关联" in page)
check("状态下拉列出新状态", "已定价待业主审" in page)

r = s.get("%s/changes" % BASE)
check("总览页说明新状态不计账",
      "已定价待业主审" in r.text and "仍是零" in r.text)

print()
print("=== 7. 待办总览会喊人去催签 ===")
from app.todo import collect


def _kinds():
    return {i["kind"]: i for i in collect(SessionLocal())}


def _delta(kind, key):
    return _kinds().get(kind, {}).get(key, 0)


# 用**增量**而不是绝对值断言：本文件前面几段留下的测试单本身也是
# 「已确认/已结算且无扫描件」，写绝对金额会跟着上一段的残留变，
# 测的就不是待办的逻辑而是执行顺序了。
before_sign = _delta("变更待业主签字", "amount")
before_scan = _delta("变更缺签字扫描件", "amount")

CO = ChangeOrder(project_id=_pid, title="__wf测试 待签", category="水电",
                 amount=50000.0, status="已定价待业主审")
NOSCAN = ChangeOrder(project_id=_pid, title="__wf测试 缺扫描件", category="泥瓦",
                     amount=8000.0, status="已确认")
SCANNED = ChangeOrder(project_id=_pid, title="__wf测试 有扫描件", category="木工",
                      amount=3000.0, status="已确认", file_path="x.pdf")
for _o in (CO, NOSCAN, SCANNED):
    db.add(_o)
db.commit()

check("待办列出「等业主签字」",
      _delta("变更待业主签字", "amount") - before_sign == 50000.0)
check("待办给出直达链接",
      "/changes?status=" in _kinds().get("变更待业主签字", {}).get("url", ""))
check("待办列出「缺签字扫描件」", _delta("变更缺签字扫描件", "amount") > before_scan)
# 关键：SCANNED 也在本批里，若它被误判为缺扫描件，增量会是 8000+3000
check("已有扫描件的不算缺（增量只含缺的那笔）",
      _delta("变更缺签字扫描件", "amount") - before_scan == 8000.0,
      str(_delta("变更缺签字扫描件", "amount") - before_scan))

r = s.get("%s/todo" % BASE)
check("待办总览页可访问", r.status_code == 200)
db.delete(CO)
db.delete(NOSCAN)
db.delete(SCANNED)
db.commit()

print()
print("=== 8. 导出带留痕 ===")
r = s.get("%s/changes/export" % BASE)
check("导出 200", r.status_code == 200)

print()
if FAILED:
    print("失败 %d 项：%s" % (len(FAILED), "、".join(FAILED)))
    sys.exit(1)
print("全部通过")