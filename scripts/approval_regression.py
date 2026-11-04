"""审批流端到端测试（临时库，绝不触碰生产数据）。

隔离方式与 scripts/perm_regression.py 一致：在 import app 之前把 app.config 的
数据目录指向临时目录，使 app.main 的模块级副作用（_migrate/seed/backup）全落在临时库。

覆盖：
  1. 未配置流程 → 提交后直接「已通过」（不打扰日常记账）
  2. 串行三关：财务 → 工程部 → 老板，必须逐关通过
  3. 非当前岗位无法审批（越权拒绝）
  4. 驳回后整单终止，提交人可重新提交
  5. 撤回 / 重新提交
  6. 重复提交被拒
  7. 审批不写任何金额（记账不受影响）
  8. 流程配置校验（未知岗位被拒）

用法：python scripts/approval_regression.py
退出码：0 全通过；1 有失败。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TMPDIR = Path(tempfile.mkdtemp(prefix="approvalreg_"))

from app import config as cfg  # noqa: E402

cfg.DATA_DIR = str(TMPDIR)
cfg.DB_PATH = str(TMPDIR / "renovation.db")
cfg.DATABASE_URL = f"sqlite:///{cfg.DB_PATH}"
cfg.UPLOAD_DIR = str(TMPDIR / "uploads")
cfg.SECRET_KEY = "approval-regression-test-only-secret"
os.makedirs(cfg.UPLOAD_DIR, exist_ok=True)

from app.approvals import (ENTITY_LABELS, ApprovalError, act, approval_state,  # noqa: E402
                           can_approve_now, is_enabled, submit, withdraw)
from app.database import SessionLocal, engine  # noqa: E402
from app.models import Base  # noqa: E402

# 建表：只建结构，不播种演示数据（测试自己造）
Base.metadata.create_all(bind=engine)
from app.models import (ApprovalFlow, ApprovalRequest, ApprovalStep,  # noqa: E402
                        Supplier, SupplierPayment, User)
from app.permissions import BUILTIN_ROLES  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = ""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra and not cond else ""))


def make_user(db, username, role):
    from app.auth import hash_password
    u = User(username=username, role=role, display_name=username, is_active=True,
             password_hash=hash_password("x"))
    db.add(u)
    db.commit()
    return u


def make_payment(db, amount=10000.0):
    s = db.query(Supplier).first()
    if s is None:
        s = Supplier(name="测试供应商")
        db.add(s)
        db.commit()
    p = SupplierPayment(supplier_id=s.id, amount=amount, pay_date=None, note="测试")
    db.add(p)
    db.commit()
    return p


def set_flow(db, entity_type, chain, enabled=True):
    f = db.query(ApprovalFlow).filter(ApprovalFlow.entity_type == entity_type).first()
    if f is None:
        f = ApprovalFlow(entity_type=entity_type)
        db.add(f)
    f.steps = json.dumps(chain, ensure_ascii=False)
    f.enabled = enabled
    db.commit()
    return f


def main():
    db = SessionLocal()

    print("=" * 62)
    print("审批流回归测试")
    print("=" * 62)

    # ---------- 前置数据 ----------
    boss = make_user(db, "boss_t", "老板")
    fin = make_user(db, "fin_t", "财务部")
    eng = make_user(db, "eng_t", "工程部")

    print("\n[1] 未配置流程 → 自动通过，不阻塞记账")
    pay = make_payment(db)
    set_flow(db, "supplier_payment", [], enabled=False)
    check("未启用审批时 is_enabled 为 False", is_enabled(db, "supplier_payment") is False)
    r = submit(db, "supplier_payment", pay.id, fin)
    check("提交后状态为已通过", r.status == "已通过", f"实际 {r.status}")
    check("未启用时不产生节点", len(r.steps) == 0)

    # ---------- 串行三关 ----------
    print("\n[2] 串行三关：财务 → 工程部 → 老板")
    pay2 = make_payment(db, 25000.0)
    set_flow(db, "supplier_payment", ["财务部", "工程部", "老板"])
    check("已启用审批", is_enabled(db, "supplier_payment") is True)
    r2 = submit(db, "supplier_payment", pay2.id, eng, "请审批")
    check("提交后状态=待审批", r2.status == "待审批", f"实际 {r2.status}")
    check("total_steps=3", r2.total_steps == 3, f"实际 {r2.total_steps}")
    check("current_step=0", r2.current_step == 0)
    check("生成了 3 个节点", len(r2.steps) == 3, f"实际 {len(r2.steps)}")
    check("节点岗位顺序正确",
          [s.role for s in sorted(r2.steps, key=lambda x: x.step_no)] == ["财务部", "工程部", "老板"])
    check("金额已快照", abs(r2.amount - 25000.0) < 0.01, f"实际 {r2.amount}")

    # 越权：工程部此刻不是当前岗位
    check("工程部此刻不能审批（当前节点是财务部）", can_approve_now(r2, eng) is False)
    try:
        act(db, r2, eng, True, "越权")
        check("越权审批被拒", False, "居然通过了")
    except ApprovalError:
        check("越权审批被拒", True)

    # 第 1 关
    db.refresh(r2)
    check("财务部可以审批当前节点", can_approve_now(r2, fin) is True)
    act(db, r2, fin, True, "金额核对无误")
    db.refresh(r2)
    check("第1关通过后 current_step=1", r2.current_step == 1, f"实际 {r2.current_step}")
    check("第1关通过后仍未完成", r2.status == "审批中", f"实际 {r2.status}")

    # 第 2 关
    check("现在工程部可以审批", can_approve_now(r2, eng) is True)
    act(db, r2, eng, True, "现场已确认")
    db.refresh(r2)
    check("第2关通过后 current_step=2", r2.current_step == 2, f"实际 {r2.current_step}")
    check("仍未完成", r2.status == "审批中", f"实际 {r2.status}")

    # 第 3 关（老板）
    check("现在只有老板能批", can_approve_now(r2, boss) is True and can_approve_now(r2, fin) is False)
    act(db, r2, boss, True, "同意")
    db.refresh(r2)
    check("第3关通过后=已通过", r2.status == "已通过", f"实际 {r2.status}")
    check("完成时间已写", r2.finished_at is not None)
    check("三关全部标记已通过",
          all(s.status == "已通过" for s in r2.steps))
    check("处理人已记录", all(s.actor_name for s in r2.steps))

    # ---------- 驳回 ----------
    print("\n[3] 驳回：整单终止，提交人可重新提交")
    pay3 = make_payment(db, 8800.0)
    r3 = submit(db, "supplier_payment", pay3.id, eng)
    try:
        act(db, r3, fin, False, "缺发票")
        check("驳回被记录", True)
    except ApprovalError as e:
        check("驳回被记录", False, str(e))
    db.refresh(r3)
    check("驳回后状态=已驳回", r3.status == "已驳回", f"实际 {r3.status}")
    check("驳回后不可再审批", can_approve_now(r3, fin) is False)
    check("驳回意见已保存", r3.result_note == "缺发票", f"实际 {r3.result_note}")
    # 重新提交
    r3 = submit(db, "supplier_payment", pay3.id, eng, "已补发票")
    check("驳回后可重新提交", r3.status == "待审批", f"实际 {r3.status}")
    check("重新提交后节点重建为3个", len(r3.steps) == 3, f"实际 {len(r3.steps)}")
    check("旧节点已清空重置",
          all(s.status == "待审批" for s in r3.steps))
    check("重新提交清空 finished_at", r3.finished_at is None)

    # ---------- 重复提交 ----------
    print("\n[4] 进行中不可重复提交")
    try:
        submit(db, "supplier_payment", pay3.id, fin)
        check("重复提交被拒", False, "居然允许了")
    except ApprovalError:
        check("重复提交被拒", True)

    # ---------- 撤回 ----------
    print("\n[5] 撤回")
    pay4 = make_payment(db, 6600.0)
    r4 = submit(db, "supplier_payment", pay4.id, eng)
    try:
        withdraw(db, r4, fin)  # 非提交人、非老板
        check("他人撤回被拒", False, "居然允许")
    except ApprovalError:
        check("他人撤回被拒", True)
    withdraw(db, r4, eng, "先缓缓")
    db.refresh(r4)
    check("提交人可撤回", r4.status == "已撤回", f"实际 {r4.status}")

    # ---------- 记账不受影响 ----------
    print("\n[6] 审批不写金额（记账不受影响）")
    before = db.query(SupplierPayment).filter(SupplierPayment.id == pay2.id).first().amount
    r2b = db.get(ApprovalRequest, r2.id)
    check("审批通过后单据金额未变", abs(before - 25000.0) < 0.01, f"实际 {before}")
    check("未生成台账行（审批不写钱）",
          db.query(SupplierPayment).filter(SupplierPayment.id == pay2.id).count() == 1)
    # 确认没有多出费用
    from app.models import CostItem
    check("审批未产生任何费用记录", db.query(CostItem).count() == 0,
          f"实际 {db.query(CostItem).count()}")

    # ---------- 状态查询 ----------
    print("\n[7] 状态摘要")
    st = approval_state(db, "supplier_payment", pay2.id)
    check("已通过单据状态可查", st["exists"] and st["status"] == "已通过",
          f"{st['exists']} {st.get('status')}")
    never = make_payment(db, 100.0)  # 从未提交过
    st2 = approval_state(db, "supplier_payment", never.id)
    check("未提交单据显示未提交", not st2["exists"] and st2["label"] == "未提交",
          f"exists={st2['exists']} label={st2['label']}")

    # ---------- 流程配置校验 ----------
    print("\n[8] 流程配置")
    set_flow(db, "purchase_contract", ["财务部", "老板"])
    check("采购合同流程可配置", is_enabled(db, "purchase_contract") is True)
    set_flow(db, "cost_item", [])
    check("未启用时 is_enabled False", is_enabled(db, "cost_item") is False)
    f = db.query(ApprovalFlow).filter(ApprovalFlow.entity_type == "purchase_contract").first()
    f.steps = "这不是JSON"
    db.commit()
    check("损坏的 JSON 不会崩溃", is_enabled(db, "purchase_contract") is False)

    # ---------- 岗位名校验 ----------
    print("\n[9] 岗位有效性")
    check("内置岗位含财务部与老板", "财务部" in BUILTIN_ROLES and "老板" in BUILTIN_ROLES)

    # ---------- 四类单据快照 ----------
    print("\n[10] 四类单据的标题/金额快照")
    from app.models import CostItem, PurchaseContract, SubContract
    from app.approvals import entity_snapshot

    # 采购合同
    pc = PurchaseContract(contract_no="HT-2026-001", name="瓷砖采购合同",
                          amount=120000.0, party_b="测试供应商")
    db.add(pc); db.commit()
    t, a = entity_snapshot(db, "purchase_contract", pc.id)
    check("采购合同标题正确", "瓷砖采购合同" in t, t)
    check("采购合同金额正确", abs(a - 120000.0) < 0.01, a)

    # 分包合同
    sc = SubContract(contract_no="FB-2026-001", name="水电分包合同",
                     amount=88000.0, party_b="水电施工队")
    db.add(sc); db.commit()
    t, a = entity_snapshot(db, "sub_contract", sc.id)
    check("分包合同标题正确", "水电分包合同" in t, t)
    check("分包合同金额正确", abs(a - 88000.0) < 0.01, a)

    # 费用（project_id 必填，演示库可能没有项目，这里补齐整条依赖链）
    from app.models import Customer, Project
    proj = db.query(Project).first()
    if proj is None:
        cust = db.query(Customer).first()
        if cust is None:
            cust = Customer(name="测试客户", status="已签约")
            db.add(cust)
            db.commit()
        proj = Project(customer_id=cust.id, name="测试项目", status="施工中")
        db.add(proj)
        db.commit()
    ci = CostItem(project_id=proj.id, category="其他费用",
                  item_name="垃圾清运费", amount=1200.0)
    db.add(ci); db.commit()
    t, a = entity_snapshot(db, "cost_item", ci.id)
    check("费用标题正确", "垃圾清运费" in t, t)
    check("费用金额正确", abs(a - 1200.0) < 0.01, a)

    # 四类单据都能走完整审批链
    print("\n[11] 四类单据都能提交并串行通过")
    for etype, obj in (("purchase_contract", pc), ("sub_contract", sc), ("cost_item", ci)):
        set_flow(db, etype, ["财务部", "老板"])
        r = submit(db, etype, obj.id, eng, "请审批")
        ok_submit = r.status == "待审批" and r.total_steps == 2
        act(db, r, fin, True, "财务同意")
        db.refresh(r)
        act(db, r, boss, True, "老板同意")
        db.refresh(r)
        check(f"{ENTITY_LABELS[etype]} 可完整走完串行审批", ok_submit and r.status == "已通过",
              f"submit={ok_submit} status={r.status}")

    # 金额在审批中不可篡改
    print("\n[12] 审批期间改单据金额不篡改已发生的审批快照")
    pc2 = PurchaseContract(name="可改金额的合同", amount=50000.0)
    db.add(pc2); db.commit()
    set_flow(db, "purchase_contract", ["财务部", "老板"])
    rp = submit(db, "purchase_contract", pc2.id, eng)
    act(db, rp, fin, True, "财务同意")
    db.refresh(rp)
    # 业务侧改金额（模拟有人偷偷改合同）
    pc2.amount = 999999.0
    db.commit()
    db.refresh(rp)
    check("改单据后审批快照金额不变", abs(rp.amount - 50000.0) < 0.01, f"快照={rp.amount}")
    act(db, rp, boss, True, "老板同意")
    check("改单据后仍可正常完成审批", rp.status == "已通过", rp.status)

    # ---------- 未审批不入账 ----------
    print("\n[13] 未审批不入账：存量数据不受影响，新单据受管控")
    from app.approvals import booking_enabled, is_booked, booked_ids
    from app.models import BLOCK_BOOKING_TYPES

    check("管控范围仅限付款与费用",
          BLOCK_BOOKING_TYPES == {"supplier_payment", "cost_item"},
          str(BLOCK_BOOKING_TYPES))

    # 关闭时：一切照旧
    f = set_flow(db, "supplier_payment", ["财务部", "老板"], enabled=True)
    f.block_booking = False
    db.commit()
    check("未开阻断时 booking_enabled=False", booking_enabled(db, "supplier_payment") is False)
    legacy_pay = make_payment(db, 7777.0)          # 从未提交审批 = 存量
    check("未开阻断时存量单据入账", is_booked(db, "supplier_payment", legacy_pay.id) is True)

    # 打开阻断
    f.block_booking = True
    db.commit()
    check("开阻断后 booking_enabled=True", booking_enabled(db, "supplier_payment") is True)
    check("存量单据（无审批记录）仍入账", is_booked(db, "supplier_payment", legacy_pay.id) is True)

    # 新提交的单据：审批前不入账
    pend = make_payment(db, 5555.0)
    rp = submit(db, "supplier_payment", pend.id, eng)
    check("待审批不入账", is_booked(db, "supplier_payment", pend.id) is False)
    act(db, rp, fin, True, "财务同意")
    db.refresh(rp)
    check("审批中仍不入账", is_booked(db, "supplier_payment", pend.id) is False)
    act(db, rp, boss, True, "老板同意")
    db.refresh(rp)
    check("审批通过后入账", is_booked(db, "supplier_payment", pend.id) is True)

    # 驳回后应退回不入账
    rej = make_payment(db, 3333.0)
    rr = submit(db, "supplier_payment", rej.id, eng)
    act(db, rr, fin, False, "缺凭证")
    db.refresh(rr)
    check("驳回后不入账", is_booked(db, "supplier_payment", rej.id) is False)

    # 合同类不受阻断影响
    set_flow(db, "purchase_contract", ["财务部", "老板"], enabled=True)
    fc = db.query(ApprovalFlow).filter(ApprovalFlow.entity_type == "purchase_contract").first()
    fc.block_booking = True   # 即使误设为 True 也应被忽略
    db.commit()
    check("合同类不因阻断开关不入账", booking_enabled(db, "purchase_contract") is False)
    check("未审批的合同仍入账", is_booked(db, "purchase_contract", pc.id) is True)

    # 批量过滤
    print("\n[14] 批量过滤与单条判定一致")
    ids = [legacy_pay.id, pend.id, rej.id]
    keep = booked_ids(db, "supplier_payment", ids)
    check("批量：存量入账", legacy_pay.id in keep)
    check("批量：已通过入账", pend.id in keep)
    check("批量：已驳回不入账", rej.id not in keep)
    check("批量结果与逐条一致",
          keep == {i for i in ids if is_booked(db, "supplier_payment", i)},
          f"batch={keep}")

    # 关闭后应全部恢复
    f.block_booking = False
    db.commit()
    check("关闭阻断后全部入账",
          booked_ids(db, "supplier_payment", ids) == set(ids))

    # ---------- 老板特权 ----------
    print("\n[15] 老板：临时流程 + 一键全通过")
    tpay = make_payment(db, 8888.0)
    r2 = submit(db, "supplier_payment", tpay.id, boss, "大额", custom_steps=["财务部", "工程部"])
    check("老板可指定临时流程", r2.total_steps == 2, r2.total_steps)
    check("临时流程节点正确",
          [s.role for s in sorted(r2.steps, key=lambda x: x.step_no)] == ["财务部", "工程部"])
    # 老板自审（act 一次走一关，两关要调两次；或用 force_pass_all 一次走完）
    act(db, r2, boss, True, "老板自批")
    db.refresh(r2)
    check("老板自审第1关后仍审批中", r2.status == "审批中", r2.status)
    act(db, r2, boss, True, "老板自批")
    db.refresh(r2)
    check("老板可自审自己提交的", r2.status == "已通过", r2.status)
    check("全部节点均已通过", all(s.status == "已通过" for s in r2.steps))
    check("两关均由老板处理", all(s.actor_id == boss.id for s in r2.steps))

    # 一键全通过
    fpay = make_payment(db, 9999.0)
    rf = submit(db, "supplier_payment", fpay.id, boss, "紧急", custom_steps=["财务部", "工程部", "老板"])
    check("三关流程创建成功", rf.total_steps == 3 and rf.status == "待审批")
    from app.approvals import force_pass_all
    force_pass_all(db, rf, boss, "老板一键通过")
    db.refresh(rf)
    check("一键全通过后状态=已通过", rf.status == "已通过", rf.status)
    check("一键通过保留逐关留痕", len(rf.steps) == 3 and all(s.actor_id == boss.id for s in rf.steps))

    # 非老板不能用这些能力
    npay = make_payment(db, 4444.0)
    try:
        submit(db, "supplier_payment", npay.id, fin, "x", custom_steps=["财务部"])
        check("非老板不能指定临时流程", False, "居然允许")
    except ApprovalError:
        check("非老板不能指定临时流程", True)
    try:
        force_pass_all(db, rf, fin)
        check("非老板不能一键全通过", False, "居然允许")
    except ApprovalError:
        check("非老板不能一键全通过", True)

    # 非法岗位
    try:
        submit(db, "supplier_payment", npay.id, boss, "x", custom_steps=["不存在的岗"])
        check("临时流程非法岗位被拒", False, "居然允许")
    except ApprovalError:
        check("临时流程非法岗位被拒", True)

    # 逗号分隔
    cpay = make_payment(db, 6666.0)
    rc = submit(db, "supplier_payment", cpay.id, boss, "x", custom_steps=["财务部，老板"])
    check("逗号分隔可解析", rc.total_steps == 2, rc.total_steps)

    db.close()

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f_ in FAIL:
            print("  失败：" + f_)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
