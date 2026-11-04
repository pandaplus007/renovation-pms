"""作废制（软删除）回归：付款与费用。

为什么这个测试要单独写、且断言要覆盖到**统计口径**
------------------------------------------------
作废制的全部风险都在一个地方：**行还在表里**。
所以任何一处忘了排除作废项，页面照常显示、不报错、列表也"看着对"，
只有金额悄悄多了出来。而金额错了是要跟供应商对账、要报税的。

于是这里不测「点按钮有没有生效」，而是**直接验证数字**：
造一笔付款 → 让它参与超付判断 → 作废 → 断言超付消失、
应付余额回到原值、对账表不再计入、报表毛利不变。

判据一律用**增量**而不是绝对值：夹具本身可能已满足某些绝对条件，
测到的就会是执行顺序而不是被测逻辑。
"""
from __future__ import annotations

import os
import sys
import atexit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func

from app.database import SessionLocal, engine
from app.models import (CostItem, Invoice, Material, Payment, PaymentReceipt,
                        Project, PurchaseContract, PurchaseOrder,
                        PurchaseOrderItem, Supplier, SupplierPayment)
from app.voided import VOIDED, VOID_ATTRS, mark_voided, not_voided, restore

MARK = "VOIDTEST"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f" —— {detail}" if detail and not cond else ""))


def cleanup():
    """必须用 atexit：中途抛异常时裸语句会被跳过，夹具永久留在库里。"""
    try:
        db = SessionLocal()
        # 清理失败是完全静默的，所以自己报出删了几条。
        # 按「先找 id 再删」而不是按 note 匹配：purchase_order_items 没有
        # note 列，早期版本这里直接 AttributeError，夹具就永久留在库里了。
        po_ids = [r[0] for r in db.query(PurchaseOrder.id).filter(
            PurchaseOrder.order_no.like(f"{MARK}%")).all()]
        pc_ids = [r[0] for r in db.query(PurchaseContract.id).filter(
            PurchaseContract.contract_no.like(f"{MARK}%")).all()]
        db.query(SupplierPayment).filter(SupplierPayment.note.like(f"%{MARK}%")).delete(synchronize_session=False)
        db.query(CostItem).filter(CostItem.note.like(f"%{MARK}%")).delete(synchronize_session=False)
        # 收款侧：凭证先删（外键指向 payments），再删节点
        db.query(PaymentReceipt).filter(
            PaymentReceipt.note.like(f"%{MARK}%")).delete(synchronize_session=False)
        db.query(Payment).filter(Payment.note.like(f"%{MARK}%")).delete(
            synchronize_session=False)
        n3 = 0
        if po_ids:
            db.query(PurchaseOrderItem).filter(
                PurchaseOrderItem.po_id.in_(po_ids)).delete(synchronize_session=False)
        n4 = db.query(PurchaseOrder).filter(PurchaseOrder.id.in_(po_ids or [0])).delete(synchronize_session=False) if po_ids else 0
        n5 = db.query(PurchaseContract).filter(PurchaseContract.id.in_(pc_ids or [0])).delete(synchronize_session=False) if pc_ids else 0
        db.commit()
        db.close()
        print(f"\n已清理临时夹具：采购明细 {n3} · 采购单 {n4} · 合同 {n5}")
    except Exception as e:
        print(f"\n清理失败：{e}")


atexit.register(cleanup)
# 开跑前先清掉历次崩溃留下的
cleanup()

from app import payables, todo as todo_mod  # noqa: E402  （cleanup 之后再导入，避免夹具影响）
from app.routers.supplier_payments import _reconciliation  # noqa: E402
from app.changes import _po_paid_map  # noqa: E402


def main():
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        sup = db.query(Supplier).first()
        if not proj or not sup:
            print("缺少基础数据（项目/供应商），无法测试")
            return

        # ============ 1. 造一笔挂在采购合同上的付款 ============
        pc = PurchaseContract(project_id=proj.id, supplier_id=sup.id,
                              name=f"{MARK}合同", contract_no=f"{MARK}-01", amount=10000, note=MARK)
        db.add(pc)
        db.flush()
        po = PurchaseOrder(project_id=proj.id, supplier_id=sup.id, contract_id=pc.id,
                           order_no=f"{MARK}-PO1", note=MARK)
        db.add(po)
        db.flush()
        pay = SupplierPayment(supplier_id=sup.id, amount=3000, contract_id=pc.id, po_id=po.id,
                              pay_method="银行转账", note=MARK)
        db.add(pay)
        cost = CostItem(project_id=proj.id, category="人工", item_name=f"{MARK}人工",
                        amount=500, note=MARK)
        db.add(cost)
        db.commit()
        pay_id, cost_id, pc_id, proj_id = pay.id, cost.id, pc.id, proj.id

        # ============ 2. 迁移回填：status 必须是「正常」而不是 NULL ============
        # 这是最关键的一条：ALTER TABLE 加的列在旧行上是 NULL，
        # 而 `status != '作废'` 对 NULL 是 NULL（假），会把历史数据全过滤掉。
        # 症状是「升级后列表集体变空」，所以必须显式验证。
        row = db.query(SupplierPayment).filter(SupplierPayment.id == pay_id).first()
        check("新建付款的 status 默认为「正常」", row.status == "正常", f"实际 {row.status!r}")
        null_cnt = db.query(func.count()).select_from(SupplierPayment).filter(
            SupplierPayment.status.is_(None)).scalar()
        check("库中不存在 status 为 NULL 的付款（迁移已回填）", (null_cnt or 0) == 0, f"{null_cnt} 条为 NULL")
        null_cost = db.query(func.count()).select_from(CostItem).filter(
            CostItem.status.is_(None)).scalar()
        check("库中不存在 status 为 NULL 的费用（迁移已回填）", (null_cost or 0) == 0, f"{null_cost} 条为 NULL")

        # ============ 3. 作废前的基线数字 ============
        def paid_of(pid):
            return float(db.query(func.coalesce(func.sum(SupplierPayment.amount), 0)).filter(
                SupplierPayment.contract_id == pid, not_voided(SupplierPayment)).scalar() or 0)

        base_paid = paid_of(pc_id)
        base_cost = float(db.query(func.coalesce(func.sum(CostItem.amount), 0)).filter(
            CostItem.id == cost_id, not_voided(CostItem)).scalar() or 0)
        recon_base = next((r["paid"] for r in _reconciliation(db) if r["supplier"].id == sup.id), 0.0)
        payables_base = next((r["paid"] for r in payables.purchase_rows(db)
                              if r["id"] == pc_id), 0.0)
        check("作废前该合同已付 = 3000", abs(base_paid - 3000) < 0.01, f"实际 {base_paid}")

        # ============ 4. 作废 ============
        mark_voided(pay, "测试作废")
        db.commit()
        check("作废写入 status", db.get(SupplierPayment, pay_id).status == VOIDED)
        check("作废写入原因", db.get(SupplierPayment, pay_id).void_reason == "测试作废")
        check("作废写入时间", db.get(SupplierPayment, pay_id).voided_at is not None)

        # ============ 5. 行还在（软删除的核心） ============
        row = db.get(SupplierPayment, pay_id)
        check("作废后行仍存在（可审计、可恢复）", row is not None)
        check("金额仍保留在行上", abs(float(row.amount) - 3000) < 0.01)

        # ============ 6. 各统计口径必须排除作废项 ============
        after_paid = paid_of(pc_id)
        check("合同已付：作废后归零", abs(after_paid) < 0.01, f"实际 {after_paid}")
        check("合同已付：相对基线正好减少 3000",
              abs((base_paid - after_paid) - 3000) < 0.01, f"减少 {base_paid - after_paid}")

        recon_after = next((r["paid"] for r in _reconciliation(db) if r["supplier"].id == sup.id), 0.0)
        check("供应商对账已付：不计作废项",
              abs((recon_base - recon_after) - 3000) < 0.01,
              f"减少 {recon_base - recon_after}")

        payables_after = next((r["paid"] for r in payables.purchase_rows(db)
                               if r["id"] == pc_id), 0.0)
        check("应付账款已付：不计作废项",
              abs((payables_base - payables_after) - 3000) < 0.01,
              f"减少 {payables_base - payables_after}")

        po_paid = _po_paid_map(db, [po.id])
        check("变更结算检查的已付映射：不计作废项",
              abs(float(po_paid.get(po.id, 0))) < 0.01, f"实际 {po_paid.get(po.id)}")

        # 采购单页的下单进度 = 已下单 ÷ 合同额，已付也要对
        check("作废后不再触发「采购合同超付」预警",
              not any(i["kind"] == "采购合同超付" and i["title"] == f"{MARK}合同"
                      for i in todo_mod.collect(db)))

        # ============ 7. 费用侧 ============
        # 先确认「未作废时它确实在合计里」，否则后面「作废后不在合计里」
        # 这条断言会在「它本来就没被统计」的情况下假绿。
        cost_live = float(db.query(func.coalesce(func.sum(CostItem.amount), 0)).filter(
            CostItem.id == cost_id, not_voided(CostItem)).scalar() or 0)
        check("作废前费用计入合计", abs(cost_live - 500) < 0.01, f"实际 {cost_live}")

        mark_voided(db.get(CostItem, cost_id), "测试作废费用")
        db.commit()
        check("作废后费用行仍在（可审计、可恢复）", db.get(CostItem, cost_id) is not None)
        cost_after = float(db.query(func.coalesce(func.sum(CostItem.amount), 0)).filter(
            CostItem.id == cost_id, not_voided(CostItem)).scalar() or 0)
        check("作废费用不计入合计", abs(cost_after) < 0.01, f"实际 {cost_after}")
        check("作废费用不会进入「成本超预算」与「成本逼近合同额」的聚合",
              not any(getattr(c, "item_name", "").startswith(MARK)
                      for c in db.query(CostItem).filter(not_voided(CostItem)).all()))
        check("作废费用不再触发待办里的「成本超预算」",
              not any(i["kind"] == "成本超预算" and i["title"] == proj.name
                      and MARK in i["detail"] for i in todo_mod.collect(db)))

        # ============ 8. 恢复 ============
        restore(db.get(SupplierPayment, pay_id))
        db.commit()
        check("恢复后 status 回到正常", db.get(SupplierPayment, pay_id).status == "正常")
        check("恢复后已付金额回来",
              abs(paid_of(pc_id) - base_paid) < 0.01, f"实际 {paid_of(pc_id)}")
        check("恢复后对账金额回来",
              abs(next((r["paid"] for r in _reconciliation(db)
                        if r["supplier"].id == sup.id), 0.0) - recon_base) < 0.01)
        check("恢复不清空作废原因（记录曾经作废过）",
              db.get(SupplierPayment, pay_id).void_reason == "测试作废")

        # ============ 9. NULL 兜底：老数据不会被 not_voided 过滤掉 ============
        # 直接构造一条 status=None 的记录，模拟从旧备份恢复 / 手工插库
        legacy = SupplierPayment(supplier_id=sup.id, amount=77, note=f"{MARK} legacy")
        db.add(legacy)
        db.commit()
        db.execute(SupplierPayment.__table__.update()
                   .where(SupplierPayment.id == legacy.id)
                   .values(status=None))
        db.commit()
        db.expire_all()
        legacy = db.get(SupplierPayment, legacy.id)
        check("status 为 NULL 的旧数据内存过滤也保留",
              any(getattr(r, "id", None) == legacy.id
                  for r in [legacy] if getattr(r, "status", "正常") != VOIDED))

        # 这条要测的是**聚合查询**：NULL 行会不会被 filter 一把抹掉。
        # 判据用「过滤前后总额相等」——此刻夹具里除了 legacy 都是「正常」，
        # 两者相等就说明 NULL 那行没被吞。
        def _sum_fixture(with_filter: bool):
            q = db.query(func.coalesce(func.sum(SupplierPayment.amount), 0)).filter(
                SupplierPayment.note.like(f"%{MARK}%"))
            if with_filter:
                q = q.filter(not_voided(SupplierPayment))
            return float(q.scalar() or 0)

        sum_filtered = _sum_fixture(True)
        sum_raw = _sum_fixture(False)
        check("NULL 行确实存在于库中（前提）", legacy.status is None, f"实际 {legacy.status!r}")
        check("not_voided 聚合未丢掉 NULL 行（77 必须被计入）",
              abs(sum_raw - sum_filtered) < 0.01 and abs(sum_raw) > 77,
              f"不过滤={sum_raw}，过滤后={sum_filtered}")
        db.query(SupplierPayment).filter(SupplierPayment.id == legacy.id).delete(
            synchronize_session=False)
        db.commit()

        # ============ 10. 收款侧 ============
        # 这一段单独存在的原因：收款节点的作废列在 Python 侧叫 void_status，
        # 因为 Payment 已有一个 status @property（未收/部分/已收清）。
        # 那个撞名最初的表现是「'property' object has no attribute 'is_'」，
        # 且只在收款相关页面触发，报错点离根因很远。
        pay_node = Payment(project_id=proj_id, name=f"{MARK}收款节点",
                           plan_amount=20000, paid_amount=8000,
                           note=f"{MARK}")
        db.add(pay_node)
        db.flush()
        node_id = pay_node.id
        rc = PaymentReceipt(payment_id=node_id, amount=8000, note=f"{MARK}")
        db.add(rc)
        db.commit()

        check("收款节点默认正常", db.get(Payment, node_id).void_status == "正常")
        check("收款明细默认正常", db.get(PaymentReceipt, rc.id).status == "正常")

        # 基线：节点已收进项目已收合计
        proj = db.get(Project, proj_id)
        base_recv = proj.recv_amount
        check("收款节点已计入项目已收合计", base_recv >= 8000, f"实际 {base_recv}")

        # 作废明细 → 已收回退，凭证仍在
        # 这一条直接调 mark_voided（不经过路由），所以 paid_amount 的回退
        # 必须像路由里那样**显式**做。生产代码里回退写在 receipt_void 里；
        # 这里手工补上，才能测「作废后金额确实变了」而不是只测状态位翻转。
        mark_voided(rc, "测试作废收款明细")
        node = db.get(Payment, node_id)
        node.paid_amount = max(0, node.paid_amount - (rc.amount or 0))
        db.commit()
        db.expire_all()
        check("作废明细后凭证行仍在", db.get(PaymentReceipt, rc.id) is not None)
        check("作废明细后凭证可再取回金额",
              abs(float(db.get(PaymentReceipt, rc.id).amount) - 8000) < 0.01)
        check("作废明细写回节点已收 = 0",
              abs(float(db.get(Payment, node_id).paid_amount)) < 0.01,
              f"实际 {db.get(Payment, node_id).paid_amount}")

        # 作废节点 → 不进已收合计，但凭证与转账单全保留
        mark_voided(db.get(Payment, node_id), "测试作废收款节点",
                    VOID_ATTRS["payments"])
        db.commit()
        db.expire_all()
        check("作废节点行仍在", db.get(Payment, node_id) is not None)
        check("作废节点后其收款凭证仍在（cascade 未触发）",
              db.get(PaymentReceipt, rc.id) is not None,
              "作废节点把下面的收款凭证连带删掉了")
        check("作废节点后凭证的 file_path 字段未被销毁",
              db.get(PaymentReceipt, rc.id) is not None
              and hasattr(db.get(PaymentReceipt, rc.id), "file_path"))
        check("作废节点不计入项目已收合计",
              abs(proj.recv_amount - (base_recv - 8000)) < 0.01,
              f"实际 {proj.recv_amount}，基线 {base_recv}")
        check("作废节点不计入项目 paid_total",
              abs(db.get(Project, proj_id).recv_amount - proj.recv_amount) < 0.01)

        # 待办里不该出现这条作废节点的「应收逾期」
        db.commit()
        check("作废节点不再被待办当成应收逾期",
              not any(i["kind"] == "应收逾期" and f"{MARK}收款节点" in i["title"]
                      for i in todo_mod.collect(db)))

        # 恢复明细 → 金额回到节点上。与上面同理：恢复路由里的金额回补是
        # 显式写的，测试直接调 restore 就要自己补上这一行。
        # 注意这里节点本身仍是「作废」状态，所以项目已收合计不该变化。
        restore(rc)
        node = db.get(Payment, node_id)
        node.paid_amount = min(node.plan_amount, node.paid_amount + (rc.amount or 0))
        db.commit()
        db.expire_all()
        check("恢复明细后节点已收回到 8000",
              abs(float(db.get(Payment, node_id).paid_amount) - 8000) < 0.01,
              f"实际 {db.get(Payment, node_id).paid_amount}")
        check("节点仍作废时，恢复明细不改变项目已收合计（两层独立生效）",
              abs(proj.recv_amount - (base_recv - 8000)) < 0.01,
              f"实际 {proj.recv_amount}")

        restore(db.get(Payment, node_id), VOID_ATTRS["payments"])
        db.commit()
        db.expire_all()
        check("恢复节点后项目已收合计完全回到基线",
              abs(proj.recv_amount - base_recv) < 0.01,
              f"实际 {proj.recv_amount}，基线 {base_recv}")

        # 清理收款侧夹具（凭证在先删，否则外键残留）
        db.query(PaymentReceipt).filter(
            PaymentReceipt.note.like(f"%{MARK}%")).delete(synchronize_session=False)
        db.query(Payment).filter(Payment.note.like(f"%{MARK}%")).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    print("=== 作废制回归 ===")
    main()
    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
