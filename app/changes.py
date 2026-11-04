"""变更签证（增项）聚合：回答「这个项目到底增了多少、增项赚没赚钱」。

为什么需要
----------
现有毛利报表的成本口径是「已下采购单 + 已下分包单」，收入口径是「施工合同额」。
装修行业里合同签完到结算之间会冒出一堆增减项——业主改需求、现场条件对不上、
图纸漏项。这部分钱不进 `contracts.amount`，于是：
- 报表上的收入偏小，成本却已经发生了 → 毛利率被算低
- 或者反过来，增项的钱收了但没进收入 → 看起来赚的比实际少

只有把「已确认的变更」单独立账，并在收入口径里加进去，毛利才接近真实。

口径约定
--------
    变更金额 amount：增项为正、减项为负（见 models.CHANGE_STATUS 附近的注释）
    计入结算：只算 status == "已确认" 或 "已结算"
        「草稿/已定价待业主审」都还没业主签字，不能进账
        「已驳回」是明确否掉的，也不进
        分界线是**业主有没有签字**，不是「流程走到哪一步」——
        「已定价待业主审」在流程上比「草稿」前进了一步，在账上却和它一样是零。
    结算金额 settle = 合同额 + 已确认变更净额
        contracts.settle_amount 有值时以它为准——那是最终结算的权威数字，
        变更表只是用来解释「结算金额为什么和合同额不一样」

变更本身的毛利
--------------
    变更毛利 = 变更金额 − 对应成本
未填成本时 ChangeOrder.profit 返回 0 而不是 amount，理由见该属性注释：
否则界面上会把「还没算成本」显示成 100% 毛利，那是误导而不是提示。
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import (CHANGE_TRANSITIONS, ChangeOrder, Contract, CostItem,
                     Project, PurchaseOrder, PurchaseOrderItem, SubContract,
                     SupplierPayment)
from .voided import not_voided

# 计入结算的变更状态。「已结算」也算——那是确认后又走完结算流程，
# 金额早已定死，只是流程上多了一步，不该把它排除。
#
# **「已定价待业主审」刻意不在这里**：业主没签字的钱不进账。
# 它属于「待签」，是流程中的金额，不是已实现的收入。
COUNTED_STATUS = ("已确认", "已结算")
# 「待签」含草稿与「已定价待业主审」。后者是已经报给业主、等签字的——
# 这笔钱可以用来催款、可以计提预期收入，但它还不是收入。
# 「已驳回」不算待签——那是明确否掉的，放在待签里会让人以为还有机会去签。
PENDING_STATUS = ("草稿", "已定价待业主审")
# 报给业主等签字的（可催款 / 可预期）
AWAITING_SIGN_STATUS = ("已定价待业主审",)


def can_transition(from_status: str, to_status: str) -> bool:
    """状态流转是否合法。终态与已驳回之外的倒退都禁止。"""
    if from_status == to_status:
        return False
    return to_status in CHANGE_TRANSITIONS.get(from_status, set())


def _po_paid_map(db: Session, po_ids: list[int]) -> dict[int, float]:
    """采购单 id → 已付金额。

    口径与 procurement/subcontracts 页一致：付款记录按 po_id 关联，
    求和。**不在这里判断「是否付清」**，只给出已付数字——
    付清的判断留给调用方，因为它要知道采购单总额（明细求和）。
    """
    if not po_ids:
        return {}
    rows = db.query(SupplierPayment.po_id, func.coalesce(
        func.sum(SupplierPayment.amount), 0.0)).filter(
        SupplierPayment.po_id.in_(po_ids), not_voided(SupplierPayment)).group_by(
        SupplierPayment.po_id).all()
    return {int(pid): round(float(v or 0), 2) for pid, v in rows}


def _po_amount_map(db: Session, po_ids: list[int]) -> dict[int, float]:
    """采购单 id → 订单金额（明细 quantity × unit_price 求和）。"""
    if not po_ids:
        return {}
    rows = db.query(PurchaseOrderItem.po_id, func.coalesce(
        func.sum(PurchaseOrderItem.quantity * PurchaseOrderItem.unit_price),
        0.0)).filter(PurchaseOrderItem.po_id.in_(po_ids)).group_by(
        PurchaseOrderItem.po_id).all()
    return {int(pid): round(float(v or 0), 2) for pid, v in rows}


def _sc_paid_map(db: Session, sc_ids: list[int]) -> dict[int, float]:
    """分包合同 id → 已付金额。"""
    if not sc_ids:
        return {}
    rows = db.query(SupplierPayment.subcontract_id, func.coalesce(
        func.sum(SupplierPayment.amount), 0.0)).filter(
        SupplierPayment.subcontract_id.in_(sc_ids), not_voided(SupplierPayment)).group_by(
        SupplierPayment.subcontract_id).all()
    return {int(sid): round(float(v or 0), 2) for sid, v in rows}


def settle_readiness(db: Session, co: ChangeOrder) -> dict:
    """转「已结算」前的检查项清单。

    为什么要做这件事
    ----------------
    「已确认」是**业主签了字**，不代表**钱已经花掉**。
    装修行业这两种状态的差距可能是几个月——签了字的单子，
    采购还没下、分包还没付。此时就标「已结算」，结算金额和实际
    支出对不上，毛利会虚高。

    返回 {"ready": bool, "items": [...], "checked": n}
    items 每项 {"label": str, "ok": bool, "detail": str}

    **不硬拦**（用户明确要求「提示后可强制转」）：现实里常出现
    「这笔变更就是不走采购」或「分包用现金结算」的情况，
    硬拦会把流程卡死。所以这里只负责**说清楚缺什么**，
    是否放行由人决定。
    """
    items: list[dict] = []

    # ---- 1. 是否关联了执行单据 ----
    po_ids = co.linked_purchase_orders
    sc_ids = co.linked_subcontracts
    cost_ids = co.linked_costs
    linked_n = len(po_ids) + len(sc_ids) + len(cost_ids)
    items.append({
        "label": "关联执行单据",
        "ok": linked_n > 0,
        "detail": ("已关联 %d 个采购单 / %d 个分包合同 / %d 个成本项"
                   % (len(po_ids), len(sc_ids), len(cost_ids)))
                   if linked_n else "一笔都没关联——无法确认这笔变更的钱花在哪了",
    })

    # ---- 2. 成本是否已录入 ----
    # cost=0 时 ChangeOrder.profit 也是 0（见 models 注释），
    # 所以「没录成本」在这套体系里等价于「这笔变更的毛利未知」。
    cost = float(co.cost or 0)
    items.append({
        "label": "对应成本已录入",
        "ok": cost > 0,
        "detail": ("已录 ¥%s" % format(cost, ",.2f")) if cost > 0
                  else "成本还是 0——没录成本时该变更的毛利显示为 0，"
                       "不是「毛利 0」，别混为一谈",
    })

    # ---- 3. 关联的采购单是否付清 ----
    if po_ids:
        amt = _po_amount_map(db, po_ids)
        paid = _po_paid_map(db, po_ids)
        unpaid_detail, all_paid = [], True
        for pid in po_ids:
            o = db.get(PurchaseOrder, pid)
            if o is None:
                unpaid_detail.append("#%d（单据已删除）" % pid)
                all_paid = False
                continue
            total = amt.get(pid, 0.0)
            p = paid.get(pid, 0.0)
            # 容差 0.5 元：分位四舍五入的误差不该让状态卡住
            remain = round(total - p, 2)
            if remain > 0.5:
                all_paid = False
                unpaid_detail.append("#%d %s（欠 ¥%s）"
                                     % (pid, o.order_no or "采购单",
                                        format(remain, ",.2f")))
        items.append({
            "label": "关联采购单已付清",
            "ok": all_paid,
            "detail": "全部付清" if all_paid
                      else "未付清：%s" % "；".join(unpaid_detail),
        })

    # ---- 4. 关联的分包合同是否付清 ----
    if sc_ids:
        paid = _sc_paid_map(db, sc_ids)
        unpaid_detail, all_paid = [], True
        for sid in sc_ids:
            sc = db.get(SubContract, sid)
            if sc is None:
                unpaid_detail.append("#%d（单据已删除）" % sid)
                all_paid = False
                continue
            p = paid.get(sid, 0.0)
            remain = round(float(sc.amount or 0) - p, 2)
            if remain > 0.5:
                all_paid = False
                unpaid_detail.append("%s（欠 ¥%s）"
                                     % (sc.name or sc.contract_no or "#%d" % sid,
                                        format(remain, ",.2f")))
        items.append({
            "label": "关联分包合同已付清",
            "ok": all_paid,
            "detail": "全部付清" if all_paid
                      else "未付清：%s" % "；".join(unpaid_detail),
        })

    # ---- 5. 关联的成本项是否存在于本项目 ----
    # 跨项目关联是数据错乱（成本项挂着别的项目的 id），不拦但要说。
    # 这里刻意**不排除作废项**：这是数据一致性检查，不是金额口径，
    # 作废的行同样不该挂到别的项目上，过滤掉反而查不出来。
    if cost_ids:
        found = db.query(CostItem).filter(CostItem.id.in_(cost_ids)).all()
        bad = [str(c.id) for c in found if c.project_id != co.project_id]
        items.append({
            "label": "成本项归属正确",
            "ok": not bad,
            "detail": "都在本项目" if not bad
                      else "这些成本项属于别的项目（可能挂错了）：%s"
                           % "、".join(bad),
        })

    # ---- 6. 有签字扫描件吗 ----
    items.append({
        "label": "签字扫描件已归档",
        "ok": bool(co.file_path),
        "detail": "已归档" if co.file_path
                  else "没有扫描件——纸质签证单是法律凭据，结算时通常要出示",
    })

    return {
        "ready": all(i["ok"] for i in items),
        "items": items,
        "checked": len(items),
    }


def _f(x) -> float:
    return float(x or 0)


def project_change_totals(db: Session) -> dict[int, dict]:
    """一次聚合算出所有项目的变更汇总（避免逐项目查询）。

    返回 {project_id: {total, confirmed, cost, profit, count, pending_count,
                    awaiting_sign_count, awaiting_sign_amount}}
    - total      全部变更净额（不论状态），用于「看到底提了多少」
    - confirmed  已确认+已结算的净额，**这个才进结算金额**
    - cost       已确认变更对应成本合计
    - profit     confirmed − cost
    - pending_count / pending_amount  未确认（草稿 + 已定价待业主审）的笔数与金额
    - awaiting_sign_count / awaiting_sign_amount  其中「已报给业主、等签字」的子集。
      单独拆出来是因为这部分**可以拿去催**（可以计提预期收入），
      而还在内部拟稿的钱什么都不是——合成一个总数就不知道该催谁。
    """
    # 按 (项目, 状态) 聚合。增/减项的切分必须在行级做，不能在这一层做——
    # 同一个状态组里既有增项又有减项（如两笔都已确认），先求和再判符号
    # 会把 -8000 和 +50000 抵成 +42000，增项减项两栏双双显示 0。
    all_rows = db.query(
        ChangeOrder.project_id,
        ChangeOrder.status,
        ChangeOrder.amount,
        ChangeOrder.cost,
    ).all()

    out: dict[int, dict] = {}
    for pid, status, amt, cost in all_rows:
        d = out.setdefault(pid, {
            "total": 0.0, "confirmed": 0.0, "cost": 0.0, "profit": 0.0,
            "count": 0, "pending_count": 0, "confirmed_count": 0,
            "rejected_count": 0,
            "add": 0.0, "cut": 0.0, "pending_amount": 0.0,
            # awaiting_sign 与 pending 分开：前者已报业主等签字，
            # 是能拿去催款、也能计提预期收入的数字；后者还在内部拟稿。
            "awaiting_sign_count": 0, "awaiting_sign_amount": 0.0,
        })
        amt, cost = _f(amt), _f(cost)
        d["total"] = round(d["total"] + amt, 2)
        d["count"] += 1
        if amt > 0:
            d["add"] = round(d["add"] + amt, 2)
        elif amt < 0:
            d["cut"] = round(d["cut"] + amt, 2)
        if status in COUNTED_STATUS:
            d["confirmed"] = round(d["confirmed"] + amt, 2)
            d["cost"] = round(d["cost"] + cost, 2)
            d["confirmed_count"] += 1
        elif status in PENDING_STATUS:
            d["pending_count"] += 1
            d["pending_amount"] = round(d["pending_amount"] + amt, 2)
            if status in AWAITING_SIGN_STATUS:
                d["awaiting_sign_count"] += 1
                d["awaiting_sign_amount"] = round(
                    d["awaiting_sign_amount"] + amt, 2)
        else:
            d["rejected_count"] += 1
    for d in out.values():
        d["profit"] = round(d["confirmed"] - d["cost"], 2)
    return out


def settle_amount(db: Session, project_id: int, contract: Contract | None) -> float:
    """项目最终结算金额。

    优先级：
      1. contracts.settle_amount 有值 → 用它（人工确认的权威数字）
      2. 否则 合同额 + 已确认变更净额（自动推导）
    """
    if contract is None:
        return 0.0
    if contract.settle_amount:
        return _f(contract.settle_amount)
    ct = project_change_totals(db).get(project_id, {})
    return round(_f(contract.amount) + _f(ct.get("confirmed", 0)), 2)


def contract_plus_confirmed(db: Session, project_id: int, contract: Contract | None) -> float:
    """合同额 + 已确认变更（**不管** settle_amount）。

    毛利报表需要这个而不是 settle_amount：settle_amount 是最终结算口径，
    在竣工结算前它通常为 0，此时报表若退回合同额就会漏掉变更。
    """
    if contract is None:
        return 0.0
    ct = project_change_totals(db).get(project_id, {})
    return round(_f(contract.amount) + _f(ct.get("confirmed", 0)), 2)


def rows_for_project(db: Session, project_id: int) -> list[ChangeOrder]:
    return (db.query(ChangeOrder)
            .filter(ChangeOrder.project_id == project_id)
            .order_by(ChangeOrder.raise_date.desc().nullslast(), ChangeOrder.id.desc())
            .all())


def category_summary(db: Session, project_id: int) -> list[dict]:
    """按变更类别汇总：看钱主要漏在哪个工序（改水电最贵）。"""
    data = db.query(
        ChangeOrder.category,
        func.coalesce(func.sum(ChangeOrder.amount), 0.0),
        func.coalesce(func.sum(ChangeOrder.cost), 0.0),
        func.count(ChangeOrder.id),
    ).filter(
        ChangeOrder.project_id == project_id,
        ChangeOrder.status.in_(COUNTED_STATUS),
    ).group_by(ChangeOrder.category).all()
    out = []
    for cat, amt, cost, cnt in data:
        amt, cost = _f(amt), _f(cost)
        out.append({
            "category": cat or "未分类", "amount": round(amt, 2),
            "cost": round(cost, 2), "count": int(cnt),
            "profit": round(amt - cost, 2),
            "rate": round((amt - cost) / amt * 100, 1) if amt else 0,
        })
    out.sort(key=lambda x: -abs(x["amount"]))
    return out


def sync_settle_amount(db: Session, project_id: int) -> float:
    """把「合同额 + 已确认变更」写回 contracts.settle_amount。

    只在 settle_amount 当前为 0（从未人工填过结算额）时才写——
    人工填的结算金额是最终权威值，不能被自动推导覆盖。
    返回最终生效的结算金额。
    """
    contract = db.query(Contract).filter(Contract.project_id == project_id).first()
    if contract is None:
        return 0.0
    if not contract.settle_amount:
        derived = contract_plus_confirmed(db, project_id, contract)
        # 变更为 0 时不要写入——那会把 settle_amount 从 NULL 变成合同额，
        # 而这两个字段在报表里的含义不同（有变更才是推导值，无变更是原值）
        if derived != _f(contract.amount):
            contract.settle_amount = derived
            db.commit()
    return settle_amount(db, project_id, contract)


def all_projects_with_changes(db: Session) -> list[dict]:
    """全项目变更总览（变更签证列表页用）。"""
    tot = project_change_totals(db)
    rows = []
    for p in db.query(Project).order_by(Project.id).all():
        ct = tot.get(p.id)
        if not ct or not ct["count"]:
            continue
        ct = dict(ct)
        ct["project"] = p
        ct["rate"] = round(ct["profit"] / ct["confirmed"] * 100, 1) if ct["confirmed"] else 0
        rows.append(ct)
    return rows