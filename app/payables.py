"""应付账款台账：回答「我一共欠供应商多少钱、近期要备多少现金」。

与既有 `_reconciliation()` 的区别
---------------------------------
`supplier_payments._reconciliation()` 按供应商汇总，应付口径是「已下采购单合计」，
不含合同额。它的场景是「跟某个供应商对账」。

本模块按**合同**维度，回答两个不同的问题：
  1. 近期确定要付多少 = 已下单 − 已付（钱已经承诺，只是还没掏）
  2. 潜在要付多少     = 合同额 − 已下单（合同签了但还没下单，不一定全要花）

为什么要分层
------------
把两者混成一个数字会误导备现金。若把合同额直接减已付，
「合同里还没下单的部分」也被算成欠款——但那笔钱可能永远不会花出去
（合同签了没下单、或最终不执行）。装修行业常见「包干总价、分期结算」，
硬算成欠款会让账面虚高。

口径定义（每份合同）
--------------------
    合同额   amount
    已下单   Σ(采购单明细 数量×单价)     分包合同为 Σ分包单金额
    已付     Σ(关联该合同的付款)
    已开票   Σ(关联该合同的进项发票，作废不计)
    近期应付 = 已下单 − 已付        ← 钱已锁定，未付
    潜在应付 = 合同额 − 已下单      ← 合同额度，尚未下单
    应付余额 = 合同额 − 已付        ← 总敞口（对不上的账：应付 > 已付）

`已开票` 不参与应付计算——发票是税务凭证，付款才是资金流出。
两者分开列，是为了看出「已付款但还没拿到发票」（进项税抵扣缺口）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import (ContractMilestone, CostItem, Invoice, Project,
                     PurchaseContract, PurchaseOrder, PurchaseOrderItem,
                     SubContract, SubOrder, Supplier, SupplierPayment)
from .voided import not_voided


def _f(x) -> float:
    return float(x or 0)


def _pct(part: float, whole: float) -> float:
    if not whole:
        return 0.0
    return round(part / whole * 100, 1)


def _next_due(db: Session, cid: int):
    """取该合同最早的、尚未过去的节点日期，作为时间参考。

    现有节点绝大多数是「交货期限」而非「付款期限」，所以这个日期**不等于付款日**——
    界面上会明确标注，避免被当成付款截止日使用。取不到就回落到签约日。
    """
    r = db.query(ContractMilestone.m_date).filter(
        ContractMilestone.contract_id == cid,
        ContractMilestone.m_date.isnot(None),
        ContractMilestone.m_date >= date.today(),
    ).order_by(ContractMilestone.m_date).first()
    return r[0] if r else None


def purchase_rows(db: Session) -> list[dict]:
    """采购合同维度的应付台账。"""
    # 一次性聚合，避免逐合同查（48 份合同 × 3 次查询 = 144 次）
    ordered_map = dict(db.query(
        PurchaseOrder.contract_id,
        func.coalesce(func.sum(PurchaseOrderItem.quantity * PurchaseOrderItem.unit_price), 0.0),
    ).join(PurchaseOrderItem, PurchaseOrderItem.po_id == PurchaseOrder.id)
        .filter(PurchaseOrder.contract_id.isnot(None))
        .group_by(PurchaseOrder.contract_id).all())

    paid_map = dict(db.query(
        SupplierPayment.contract_id,
        func.coalesce(func.sum(SupplierPayment.amount), 0.0),
    ).filter(SupplierPayment.contract_id.isnot(None), not_voided(SupplierPayment))
        .group_by(SupplierPayment.contract_id).all())

    invoiced_map = dict(db.query(
        Invoice.contract_id,
        func.coalesce(func.sum(Invoice.amount), 0.0),
    ).filter(Invoice.contract_id.isnot(None), Invoice.status != "作废")
        .group_by(Invoice.contract_id).all())

    proj_map = {p.id: p for p in db.query(Project).all()}
    sup_map = {s.id: s for s in db.query(Supplier).all()}

    out = []
    for c in db.query(PurchaseContract).order_by(PurchaseContract.id).all():
        amount = _f(c.amount)
        ordered = _f(ordered_map.get(c.id))
        paid = _f(paid_map.get(c.id))
        invoiced = _f(invoiced_map.get(c.id))
        out.append(_row(
            kind="purchase", obj=c, amount=amount, ordered=ordered,
            paid=paid, invoiced=invoiced,
            who=(sup_map.get(c.supplier_id).name if sup_map.get(c.supplier_id)
                 else (c.party_b or "未指定供应商")),
            project=(proj_map.get(c.project_id).name if proj_map.get(c.project_id) else ""),
            no_delivery=bool(c.no_delivery),
            db=db, cid=c.id,
        ))
    return out


def subcontract_rows(db: Session) -> list[dict]:
    """分包合同维度的应付台账。"""
    ordered_map = dict(db.query(
        SubOrder.subcontract_id,
        func.coalesce(func.sum(SubOrder.total_amount), 0.0),
    ).group_by(SubOrder.subcontract_id).all())

    paid_map = dict(db.query(
        SupplierPayment.subcontract_id,
        func.coalesce(func.sum(SupplierPayment.amount), 0.0),
    ).filter(SupplierPayment.subcontract_id.isnot(None), not_voided(SupplierPayment))
        .group_by(SupplierPayment.subcontract_id).all())

    proj_map = {p.id: p for p in db.query(Project).all()}

    out = []
    for s in db.query(SubContract).order_by(SubContract.id).all():
        amount = _f(s.amount)
        ordered = _f(ordered_map.get(s.id))
        paid = _f(paid_map.get(s.id))
        # 分包合同没有发票关联字段，固定为 0（不臆造数据）
        out.append(_row(
            kind="sub", obj=s, amount=amount, ordered=ordered, paid=paid,
            invoiced=0.0,
            who=(s.party_b or "未指定分包商"),
            project=(proj_map.get(s.project_id).name if proj_map.get(s.project_id) else ""),
            no_delivery=True,
            db=db, cid=s.id,
        ))
    return out


def _row(kind, obj, amount, ordered, paid, invoiced, who, project,
         no_delivery, db, cid) -> dict:
    due_payable = round(ordered - paid, 2)        # 近期：已下单未付
    potential = round(amount - ordered, 2)       # 潜在：未下单部分
    balance = round(amount - paid, 2)            # 总敞口
    ref_date = _next_due(db, cid) or obj.sign_date
    return {
        "kind": kind,
        "id": obj.id,
        "no": (obj.contract_no or ""),
        "name": obj.name or ("合同#%s" % obj.id),
        "who": who,
        "project": project,
        "amount": round(amount, 2),
        "ordered": round(ordered, 2),
        "paid": round(paid, 2),
        "invoiced": round(invoiced, 2),
        "due_payable": due_payable,      # 已下单未付
        "potential": potential,          # 未下单
        "balance": balance,              # 合同额 − 已付
        "over_paid": round(-balance, 2) if balance < -0.01 else 0.0,
        "settle_pct": _pct(paid, amount),
        "order_pct": _pct(ordered, amount),
        # 已付但没票 → 进项税抵扣缺口
        "invoice_gap": round(paid - invoiced, 2) if paid > invoiced + 0.01 else 0.0,
        "ref_date": ref_date,
        "url": ("/contracts/%d" % cid) if kind == "purchase"
               else ("/subcontracts/%d" % cid),
        "no_delivery": no_delivery,
    }


def build(db: Session, kind: str = "all") -> dict:
    """汇总台账。kind: all / purchase / sub。"""
    rows = []
    if kind in ("all", "purchase"):
        rows += purchase_rows(db)
    if kind in ("all", "sub"):
        rows += subcontract_rows(db)

    # 排序：近期应付（已下单未付，真正要出的钱）降序，同档再按总敞口排。
    # 注意不能用 max(x, 0) 做主键——那会把所有超付合同压成 0，与「刚好结清」
    # 混成一档，次级键（敞口）反而把它们顶到前面，出现「0 之后又冒出正数」。
    rows.sort(key=lambda r: (-r["due_payable"], -r["balance"]))

    def s(key):
        return round(sum(r[key] for r in rows), 2)

    # 已付 > 已下单 的合同：钱付了但没录下单。这些行 due_payable 为负，
    # 在「近期应付」汇总里被 max(0) 剔除，导致「近期+潜在」与「总敞口」不等。
    # 把差额单独算出来，界面上说明，否则看数字会以为算错了。
    over_order = [r for r in rows if r["due_payable"] < -0.01]

    return {
        "rows": rows,
        "total_amount": s("amount"),
        "total_ordered": s("ordered"),
        "total_paid": s("paid"),
        "total_invoiced": s("invoiced"),
        # 近期确定要付：已下单 − 已付，负数（已付超下单）不计入
        "total_due": round(sum(max(r["due_payable"], 0) for r in rows), 2),
        # 潜在应付：合同额 − 已下单
        "total_potential": round(sum(max(r["potential"], 0) for r in rows), 2),
        # 总敞口
        "total_balance": s("balance"),
        "total_over_paid": s("over_paid"),
        "total_invoice_gap": s("invoice_gap"),
        "count": len(rows),
        "count_unpaid": sum(1 for r in rows if r["due_payable"] > 0.5),
        "count_settled": sum(1 for r in rows if r["amount"] > 0 and r["balance"] <= 0.5),
        # 已付超下单的合同
        "over_order_count": len(over_order),
        "over_order_rows": over_order,
        "over_order_amount": round(sum(-r["due_payable"] for r in over_order), 2),
    }


def by_party(rows: list[dict]) -> list[dict]:
    """按收付款方汇总（供应商 / 分包商），回答「这家一共欠多少」。

    与合同维度互补：合同多的时候看这一张。
    """
    agg: dict = {}
    for r in rows:
        k = r["who"] or "未指定"
        a = agg.setdefault(k, {
            "who": k, "amount": 0.0, "ordered": 0.0, "paid": 0.0,
            "due_payable": 0.0, "potential": 0.0, "balance": 0.0, "count": 0,
        })
        for f in ("amount", "ordered", "paid", "due_payable", "potential", "balance"):
            a[f] += r[f]
        a["count"] += 1
    out = []
    for v in agg.values():
        v["settle_pct"] = _pct(v["paid"], v["amount"])
        out.append(v)
    out.sort(key=lambda x: (-x["due_payable"], -x["balance"]))
    return out