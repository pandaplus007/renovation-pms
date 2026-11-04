"""待办与异常聚合：把散落在各模块的「该管一管的事」收拢到一屏。

为什么做这个
------------
系统有 20 个模块、43 张表，很多功能其实早就写好了——采购合同的送货单、
项目成本对比、客户跟进提醒，页面上都有。但它们各自藏在自己模块的 Tab 里，
需要你先想到「今天该看看哪个模块」才看得见。现实是：不做那波集中补录之后，
就没人再打开过。

所以这里不新增业务逻辑，只做一件事：**扫描已存在的数据，把不该被忽略的异常
集中列出来**，每条都带直达链接。

设计原则
--------
1. **只报异常，不报正常**。已付清、已验收、库存充足——这些不该出现在待办里。
2. **按金额/紧迫度排序**，最该先看的排最前。
3. **每条必须可点击直达**，不给人「看到问题还得自己去翻」的二次劳动。
4. 查询全部走索引列，避免全表扫描；条数用 SQL 聚合而非取回全部行再数。
"""
from __future__ import annotations

import urllib.parse
from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .changes import AWAITING_SIGN_STATUS, COUNTED_STATUS
from .models import (BudgetItem, ChangeOrder, CostItem, Customer, DeliveryNote,
                     FollowUp, Material, Payment, Project, PurchaseContract,
                     SubContract, SubOrder, SupplierPayment, Task)
from .row_scope import visible_projects
from .voided import VOID_ATTRS, not_voided


def _days_overdue(d) -> int | None:
    """距今天数。列类型可能是 date 或 datetime，统一按日期算。"""
    if not d:
        return None
    if isinstance(d, datetime):
        d = d.date()
    return (date.today() - d).days


def _fmt_date(d) -> str:
    """统一日期显示：date → YYYY-MM-DD，datetime → YYYY-MM-DD。"""
    if not d:
        return "—"
    if isinstance(d, datetime):
        d = d.date()
    return str(d)


def _scan(db: Session, user=None, project_ids=None) -> list[dict]:
    """扫描确定性的异常（逾期、超付、缺口、未跟进、库存…）。

    只报确定存在的问题；「接近出问题」的阈值类预警在 threshold_alerts()，
    两者的判定口径不同，分开写更清楚。

    `user` 用于行级权限：待办是无差别入口，不按可见范围过滤的话，
    收紧范围的用户仍会在这里看到别人项目的客户名与欠款金额。
    不传时（CLI 脚本、企微推送的全量视图）行为与从前完全一致。

    `project_ids` 用于**按项目收窄**（经营大屏的单项目视图）。
    它**不能**放宽行级权限：有效集合永远是两者的交集。
    不传 = 不收窄，所有既有调用方行为不变。

    ⚠ 几条**跨项目**的条目在收窄时会整体消失（见下面各处 `if SCOPED`）：
       库存预警、变更待业主签字、客户未跟进。这不是漏过滤，
       而是它们天生就是全公司口径的一句话（「18 种材料低于预警库存」），
       在单项目视图里说这句话只会误导——老板会以为这 18 种只跟这个项目有关。
    """
    items: list[dict] = []
    SCOPED = project_ids is not None

    # ---- 行级权限：可见项目 id 集合 ----
    #
    # 待办是**无差别入口**（首页就能看到），且每条都带项目名、客户名、
    # 金额与超支差额。不按范围收窄的话，收紧范围的员工照样在这里
    # 看到全公司的情况——行级权限就白做了。
    #
    # 不传 user 时（CLI、企微每日推送）返回 None = 不限制，维持全量视图。
    if user is None:
        VIS = None
    else:
        _ids = {p.id for p in visible_projects(db, user, Project)}
        VIS = _ids or set()      # 空集必须保持「空」，不能变成 None（全量）
    # 收窄到指定项目。**取交集，不取覆盖**：传进来的 id 只能进一步缩小范围，
    # 不能把不可见的项目「放回来」。写成 VIS = set(project_ids) 就是权限绕过。
    if project_ids is not None:
        _want = set(project_ids)
        VIS = _want if VIS is None else (VIS & _want)

    def _visible_project(pid) -> bool:
        """项目是否该出现在这条待办里。VIS 为 None 表示不限制。"""
        return True if VIS is None else (pid in VIS)

    # ---------- 1. 客户欠款逾期 ----------
    # 排除作废节点：作废的节点不该被当成「客户欠钱」——那会直接变成
    # 一条要去催款的待办，而这条账其实已经不算了。
    overdue = db.query(Payment).filter(
        Payment.plan_date.isnot(None),
        Payment.plan_date < date.today(),
        func.coalesce(Payment.plan_amount, 0) > func.coalesce(Payment.paid_amount, 0),
        not_voided(Payment, VOID_ATTRS["payments"]),
    ).all()
    for p in overdue:
        if not _visible_project(p.project_id):
            continue
        bal = (p.plan_amount or 0) - (p.paid_amount or 0)
        proj = db.get(Project, p.project_id) if p.project_id else None
        items.append({
            "level": "danger",
            "kind": "应收逾期",
            "title": p.name or "未命名收款",
            "detail": f"计划 {_fmt_date(p.plan_date)}，已逾期 {_days_overdue(p.plan_date)} 天"
                       + (f" · {proj.name}" if proj else ""),
            "amount": bal, "count": 1,
            "url": f"/projects/{p.project_id}#payments" if p.project_id else "/reports/ledger",
        })

    # ---------- 2. 超付：已付 > 合同额 ----------
    # 已付一律排除作废单据：作废的付款是「录错了不算数」，拿它算超付会
    # 报出一笔根本不存在的超付，工人跑去核账发现没有，比不报更伤信任。
    for c in db.query(PurchaseContract).filter(PurchaseContract.amount > 0).all():
        if not _visible_project(c.project_id):
            continue
        paid = db.query(func.coalesce(func.sum(SupplierPayment.amount), 0)).filter(
            SupplierPayment.contract_id == c.id, not_voided(SupplierPayment)).scalar() or 0
        if paid > c.amount + 1:
            items.append({
                "level": "danger", "kind": "采购合同超付",
                "title": c.name,
                "detail": f"合同额 {c.amount:,.0f}，已付 {paid:,.0f}",
                "amount": paid - c.amount, "count": 1,
                "url": f"/contracts/{c.id}",
            })
    for s in db.query(SubContract).filter(SubContract.amount > 0).all():
        if not _visible_project(s.project_id):
            continue
        paid = db.query(func.coalesce(func.sum(SupplierPayment.amount), 0)).filter(
            SupplierPayment.subcontract_id == s.id, not_voided(SupplierPayment)).scalar() or 0
        if paid > s.amount + 1:
            items.append({
                "level": "danger", "kind": "分包合同超付",
                "title": s.name,
                "detail": f"合同额 {s.amount:,.0f}，已付 {paid:,.0f}",
                "amount": paid - s.amount, "count": 1,
                "url": f"/subcontracts/{s.id}",
            })

    # ---------- 3. 需送货但无送货单 ----------
    # 一次查出「需送货」与「已有送货」两个集合做差，避免逐份查送货单。
    need_ids = {r[0] for r in db.query(PurchaseContract.id).filter(
        PurchaseContract.no_delivery.is_(False)).all()}
    got_ids = {r[0] for r in db.query(DeliveryNote.contract_id).filter(
        DeliveryNote.contract_id.isnot(None)).all()}
    pending_delivery = need_ids - got_ids
    if pending_delivery:
        rows = db.query(PurchaseContract).filter(
            PurchaseContract.id.in_(pending_delivery)).all()
        # 行级权限：详情里带合同名与合同额，只留可见项目的
        rows = [c for c in rows if _visible_project(c.project_id)]
    if pending_delivery and rows:
        total = sum(c.amount or 0 for c in rows)
        items.append({
            "level": "warning", "kind": "待登记送货",
            "title": f"{len(rows)} 份采购合同尚未登记送货单",
            "detail": "收到货后登记，系统才能算出实际到货与合同差额。"
                       + "（金额最大：" + max(rows, key=lambda c: c.amount or 0).name + "）",
            "amount": total, "count": len(rows),
            "url": "/procurement?filter=no_delivery",
        })

    # ---------- 4. 分包合同下单缺口 ----------
    # 合同额 > 已下分包单：可能是尚未下单，也可能合同含包干总额不需细分。
    # 一律提示，不判定对错——由人决定要不要拆单。
    for s in db.query(SubContract).filter(SubContract.amount > 0).all():
        if not _visible_project(s.project_id):
            continue
        ordered = db.query(func.coalesce(func.sum(SubOrder.total_amount), 0)).filter(
            SubOrder.subcontract_id == s.id).scalar() or 0
        gap = (s.amount or 0) - ordered
        if gap > 1:
            # 标题带分包商：多个合同可能同名（「空调设备采购合同」就有两份），
            # 只看合同名分不清是谁的
            who = (s.party_b or "").strip()
            label = f"{s.name}" + (f" · {who[:16]}" if who else "")
            items.append({
                "level": "warning", "kind": "分包下单缺口",
                "title": label,
                "detail": f"合同额 {s.amount:,.0f}，已下分包单 {ordered:,.0f}，"
                          f"差 {gap:,.0f}（未拆单或未下单）",
                "amount": gap, "count": 1,
                "url": f"/subcontracts/{s.id}",
            })

    # ---------- 5. 项目成本超预算 ----------
    # 一次性把所有项目的预算/成本聚合出来，避免逐项目两次查询（N+1）。
    budget_map = dict(db.query(BudgetItem.project_id,
                               func.coalesce(func.sum(BudgetItem.budget_amount), 0))
                      .group_by(BudgetItem.project_id).all())
    cost_map = dict(db.query(CostItem.project_id,
                             func.coalesce(func.sum(CostItem.amount), 0))
                    .filter(not_voided(CostItem))
                    .group_by(CostItem.project_id).all())
    # 行级权限：用开头算好的 VIS（聚合 map 已按 project_id 组织，
    # 所以直接按 id 过滤比再查一遍 Project 更省）
    for p in db.query(Project).all():
        if not _visible_project(p.id):
            continue
        budget = budget_map.get(p.id, 0) or 0
        if budget <= 0:
            continue
        cost = cost_map.get(p.id, 0) or 0
        if cost > budget:
            items.append({
                "level": "danger", "kind": "成本超预算",
                "title": p.name,
                "detail": f"预算 {budget:,.0f}，已发生 {cost:,.0f}，超 {cost - budget:,.0f}",
                "amount": cost - budget, "count": 1,
                "url": f"/projects/{p.id}",
            })

    # ---------- 6. 项目超期未竣工 ----------
    for p in db.query(Project).filter(
            Project.end_date.isnot(None),
            Project.end_date < date.today(),
            Project.status.notin_(["已验收", "售后"])).all():
        if not _visible_project(p.id):
            continue
        items.append({
            "level": "warning", "kind": "工期逾期",
            "title": p.name,
            "detail": f"计划竣工 {_fmt_date(p.end_date)}，已超期 {_days_overdue(p.end_date)} 天（{p.status}）",
            "amount": 0, "count": 1,
            "url": f"/projects/{p.id}",
        })

    # ---------- 7. 客户跟进 ----------
    # 注意：next_follow_at 声明为 Date，但历史数据里存 datetime，SQL 层面比较
    # 会抛 "<' not supported between date and datetime"。取回后在 Python 里归一。
    all_fu = db.query(FollowUp).filter(FollowUp.next_follow_at.isnot(None)).all()
    # 跟进记录挂在客户上，而客户可以属于多个项目。判据：该客户名下
    # **至少有一个可见项目**。客户没项目（纯线索）时不显示——
    # 行级权限管的是「项目数据」，纯线索不属于任何项目，无从判断该不该给看。
    _cust_vis: dict[int, bool] = {}

    def _cust_visible(cid) -> bool:
        if cid in _cust_vis:
            return _cust_vis[cid]
        if VIS is None:
            _cust_vis[cid] = True
            return True
        # 客户与项目的关联就是 Project.customer_id（没有中间表）
        hit = db.query(Project.id).filter(
            Project.customer_id == cid, Project.id.in_(VIS)).first()
        _cust_vis[cid] = bool(hit)
        return _cust_vis[cid]

    due = []
    for f in all_fu:
        d = f.next_follow_at
        if hasattr(d, "date"):
            d = d.date()
        if d < date.today() and _cust_visible(f.customer_id):
            due.append((d, f))
    due.sort(key=lambda t: t[0])
    for d, f in due:
        cust = db.get(Customer, f.customer_id) if f.customer_id else None
        items.append({
            "level": "warning", "kind": "客户待跟进",
            "title": (cust.name if cust else "客户") ,
            "detail": f"跟进计划 {_fmt_date(d)}，已到期 {_days_overdue(d)} 天"
                       + (f" · {(f.content or '').strip()[:30]}" if (f.content or "").strip() else ""),
            "amount": 0, "count": 1,
            "url": f"/customers/{f.customer_id}" if f.customer_id else "/customers",
        })
    # 从未跟进过、且还停在线索阶段的
    never = db.query(Customer).filter(
        Customer.status.notin_(["已签约", "已流失"]),
        ~Customer.id.in_(db.query(FollowUp.customer_id).filter(
            FollowUp.customer_id.isnot(None)))
    ).all()
    if VIS is not None:
        never = [c for c in never if _cust_visible(c.id)]
    if never and not SCOPED:     # 收窄到单项目时它必然为空，但显式挡掉更清楚
        items.append({
            "level": "info", "kind": "客户未跟进",
            "title": f"{len(never)} 个客户还没有任何跟进记录",
            "detail": "线索放着不跟进就不会成交。",
            "amount": 0, "count": len(never),
            "url": "/customers",
        })

    # ---------- 8. 库存低于预警线 ----------
    low = db.query(Material).filter(
        Material.low_stock > 0,
        Material.stock.isnot(None),
        Material.stock <= Material.low_stock).all()
    if low and not SCOPED:
        items.append({
            "level": "warning", "kind": "库存预警",
            "title": f"{len(low)} 种材料低于预警库存",
            "detail": "最近的一项：" + low[0].name
                       + f"（现有 {low[0].stock:g}{low[0].unit or ''} / 预警 {low[0].low_stock:g}）",
            "amount": 0, "count": len(low),
            "url": "/inventory",
        })

    # ---------- 9. 变更签证等业主签字 ----------
    # 「已定价待业主审」是唯一**能主动推一把**的待办金额：钱已经报出去了，
    # 球在业主那边，我方能做的就是催。不列在这里，这些单会一直躺在
    # 「待确认」里直到业主想起来为止。
    # 严格说这不是「异常」而是「待办」，但待办总览就是收这类事的页面，
    # 放到变更签证页里等人主动去看，那和没做一样。
    # 库存与变更签字**刻意不按行级权限过滤**：
    #   - 库存预警是全局资源预警（材料不属于任何项目），对每个有采购权限的人
    #     都有用，限制它反而让人备料时看不到预警；
    #   - 变更待业主签字是「对外催办」事项，同一批单子往往由老板/工程部
    #     跨项目推进，收紧范围反而催不动。
    # 这两条是刻意的例外，不是遗漏，见 app/row_scope.py 顶部说明。
    awaiting = db.query(ChangeOrder).filter(
        ChangeOrder.status.in_(AWAITING_SIGN_STATUS)).all()
    # 收窄到单项目时，把待签字的变更也限到该项目——否则在项目视图里
    # 仍会出现「全公司 12 笔变更等业主签字」，而这句话与当前项目无关。
    # 这一条**刻意不受行级权限过滤**（见上面的注释），所以收窄只能靠这里。
    if SCOPED:
        _want = set(project_ids)
        awaiting = [o for o in awaiting if o.project_id in _want]
    if awaiting:
        total = sum(o.amount or 0 for o in awaiting)
        by_proj: dict[int, list] = {}
        for o in awaiting:
            by_proj.setdefault(o.project_id, []).append(o)
        # 按金额从大到小：先催金额最大的那几笔
        worst = max(awaiting, key=lambda o: abs(o.amount or 0))
        wproj = db.get(Project, worst.project_id) if worst.project_id else None
        items.append({
            "level": "warning", "kind": "变更待业主签字",
            "title": f"{len(awaiting)} 笔变更签证等业主签字",
            "detail": f"合计 {total:,.0f}，涉及 {len(by_proj)} 个项目。"
                       + "已定价报出去，钱可以去催——但业主签字前不进结算金额。"
                       + (f"（金额最大：" + (wproj.name if wproj else "")
                          + f" {worst.change_no or worst.title} {worst.amount:,.0f}）"
                          if worst.amount else ""),
            "amount": total, "count": len(awaiting),
            "url": "/changes?status=" + urllib.parse.quote(AWAITING_SIGN_STATUS[0]),
        })

    # ---------- 9.5 任务逾期 ----------
    # 为什么放在这里而不是只在任务页显示：待办总览的定位是「今天该管一管的事」。
    # 一个任务过了截止日还没做完，正是该管的事；而任务页只有主动打开才看得到，
    # 现实里没人天天打开它——所以必须在这里冒头。
    #
    # 判据复用 Task.is_overdue（未完成 + 有截止日 + 已过期），不另写一份：
    # 两处口径不一致会出现「任务页说没逾期、待办说逾期了」的矛盾。
    # 逾期任务只收**可见项目**下的那些：待办总览是无差别入口，
    # 里面出现别人项目的任务名，行级权限就白做了。
    # VIS 是 _scan() 开头算好的集合（None = 不限制）。
    if VIS is None:
        overdue_tasks = db.query(Task).filter(
            Task.due_date.isnot(None),
            Task.due_date < date.today(),
            Task.status.notin_(("已完成", "已取消")),
        ).all()
    elif not VIS:
        overdue_tasks = []
    else:
        overdue_tasks = db.query(Task).filter(
            Task.project_id.in_(VIS),
            Task.due_date.isnot(None),
            Task.due_date < date.today(),
            Task.status.notin_(("已完成", "已取消")),
        ).all()
    if overdue_tasks:
        rows = []
        by_proj: dict[int, list] = {}
        for t in overdue_tasks:
            proj = db.get(Project, t.project_id) if t.project_id else None
            rows.append((t, proj))
            by_proj.setdefault(t.project_id, []).append(t)
        # 按逾期天数从多到少：拖得最久的先做。
        rows.sort(key=lambda p: p[0].due_date or date.today())
        worst, wproj = rows[0]
        items.append({
            "level": "warning", "kind": "任务逾期",
            "title": f"{len(overdue_tasks)} 条任务已过截止日",
            "detail": f"涉及 {len(by_proj)} 个项目。最久的一条："
                       + (f"{(wproj.name + ' · ') if wproj else ''}"
                          f"{worst.title}（{worst.assignee or '未指派'}，"
                          f"逾期 {_days_overdue(worst.due_date)} 天）"),
            "amount": 0, "count": len(overdue_tasks),
            "url": "/tasks?who=overdue",
        })

    # ---------- 10. 已确认但缺签字扫描件 ----------
    # 纸质签证单是法律凭据。只记了「已确认」而扫描件没传，等于
    # 账面有这笔钱、手上拿不出证据——结算或扯皮时会很被动。
    no_scan = db.query(ChangeOrder).filter(
        ChangeOrder.status.in_(COUNTED_STATUS),
        (ChangeOrder.file_path.is_(None) | (ChangeOrder.file_path == ""))).all()
    if no_scan:
        total = sum(o.amount or 0 for o in no_scan)
        items.append({
            "level": "warning", "kind": "变更缺签字扫描件",
            "title": f"{len(no_scan)} 笔已确认变更没有签字扫描件",
            "detail": "纸质签证单是法律凭据，只记状态不传扫描件等于账实无据。"
                       + (f"（合计 {total:,.0f}）" if total else ""),
            "amount": total, "count": len(no_scan),
            "url": "/changes",
        })

    return items


# ---------- 阈值预警 ----------
# 与「已超付」的确定性判断不同，这里是**提前预警**：还没出问题，但按当前进度
# 走下去会出问题。阈值从 data/alerts.json 读，没配就用默认值。
def _load_thresholds() -> dict:
    """读取阈值配置。格式 {"cost_ratio": 0.8, "receive_ratio": 0.9, ...}。"""
    import json
    import os
    from .config import DATA_DIR
    path = os.path.join(DATA_DIR, "alerts.json")
    defaults = {"cost_ratio": 0.8, "receive_ratio": 0.9, "pay_ratio": 0.9}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in defaults:
                if isinstance(data.get(k), (int, float)) and 0 < float(data[k]) <= 3:
                    defaults[k] = float(data[k])
    except Exception:
        pass
    return defaults


def threshold_alerts(db: Session, user=None, project_ids=None) -> list[dict]:
    """阈值类预警（毛利/收款/付款比例逼近上限）。

    `user` 用于行级权限：这些预警里带着合同额与预计毛利，是最敏感的数字，
    不可见的项目绝不能出现在别人的待办里。

    `project_ids` 的语义与 `_scan` 相同，且同样是**收窄而非放宽**。
    """
    SCOPED = project_ids is not None
    th = _load_thresholds()
    out = []

    # 项目成本 / 合同额
    from .models import Contract, PurchaseOrder, PurchaseOrderItem, SubContract, SubOrder
    contract_map = dict(db.query(Contract.project_id, Contract.amount).all())
    purchase_map = dict(db.query(
        PurchaseOrder.project_id,
        func.coalesce(func.sum(PurchaseOrderItem.quantity * PurchaseOrderItem.unit_price), 0.0),
    ).join(PurchaseOrderItem, PurchaseOrderItem.po_id == PurchaseOrder.id)
        .filter(PurchaseOrder.project_id.isnot(None))
        .group_by(PurchaseOrder.project_id).all())
    sub_map = dict(db.query(
        SubContract.project_id,
        func.coalesce(func.sum(SubOrder.total_amount), 0.0),
    ).join(SubOrder, SubOrder.subcontract_id == SubContract.id)
        .filter(SubContract.project_id.isnot(None))
        .group_by(SubContract.project_id).all())
    cost_map = dict(db.query(CostItem.project_id, func.coalesce(func.sum(CostItem.amount), 0))
                    .filter(not_voided(CostItem))
                    .group_by(CostItem.project_id).all())

    # 行级权限：只遍历可见项目。这两条预警带着合同额与预计毛利，
    # 泄露出去等于把别人的经营数据摆在待办里。
    # VIS 复用 _scan() 开头算好的集合，避免第二次查 Project。
    if user is None:
        scan_projects = db.query(Project).all()
    else:
        _t_vis = {p.id for p in visible_projects(db, user, Project)}
        scan_projects = (db.query(Project).filter(Project.id.in_(_t_vis)).all()
                         if _t_vis else [])
    # 取交集，不能覆盖（理由同 _scan 里那行注释）
    if SCOPED:
        _want = set(project_ids)
        scan_projects = [p for p in scan_projects if p.id in _want]

    for p in scan_projects:
        revenue = float(contract_map.get(p.id) or 0)
        if revenue <= 0:
            continue
        cost = float(purchase_map.get(p.id, 0)) + float(sub_map.get(p.id, 0)) + float(cost_map.get(p.id, 0))
        ratio = cost / revenue
        if ratio >= th["cost_ratio"]:
            level = "danger" if ratio >= 1 else "warning"
            margin = revenue - cost
            out.append({
                "level": level, "kind": "成本逼近合同额" if ratio < 1 else "成本已超合同额",
                "title": p.name,
                "detail": f"合同额 {revenue:,.0f}，累计成本 {cost:,.0f}"
                          f"（{ratio * 100:.0f}%），"
                          + (f"预计毛利 {margin:,.0f}" if margin >= 0 else f"已亏 {margin:,.0f}"),
                "amount": max(0.0, -margin) or margin,
                "count": 1,
                "url": f"/projects/{p.id}",
            })

    # 收款进度落后
    recv_map = dict(db.query(Payment.project_id,
                              func.coalesce(func.sum(Payment.paid_amount), 0))
                     .filter(not_voided(Payment, VOID_ATTRS["payments"]))
                     .group_by(Payment.project_id).all())
    for p in scan_projects:      # 与上面同一个可见集合，见 threshold_alerts 开头
        revenue = float(contract_map.get(p.id) or 0)
        if revenue <= 0:
            continue
        received = float(recv_map.get(p.id) or 0)
        ratio = received / revenue
        if ratio >= th["receive_ratio"]:
            out.append({
                "level": "warning", "kind": "收款进度落后",
                "title": p.name,
                "detail": f"合同额 {revenue:,.0f}，已收 {received:,.0f}"
                          f"（{ratio * 100:.0f}%），待收 {revenue - received:,.0f}",
                "amount": revenue - received, "count": 1,
                "url": f"/projects/{p.id}",
            })

    return out


def collect(db: Session, user=None, project_ids=None) -> list[dict]:
    """扫描全部异常并按严重度排序返回。

    传 `user` 时按其数据可见范围过滤（行级权限，见 app/row_scope.py）。
    不传时返回全量——CLI 与每日企微推送走这条路径，因为它们是「老板视角」，
    而推送内容也只包含金额与项目名，不含客户联系人。

    `project_ids` 收窄到指定项目（经营大屏的单项目视图），语义见 `_scan`。
    """
    items = _scan(db, user, project_ids)
    # 阈值预警（成本逼近合同额、收款落后）单独一组，判定口径不同
    items.extend(threshold_alerts(db, user, project_ids))
    order = {"danger": 0, "warning": 1, "info": 2}
    items.sort(key=lambda x: (order.get(x["level"], 3), -abs(x["amount"] or 0)))
    return items


def summary(db: Session, user=None, project_ids=None) -> dict:
    """按等级统计条数与涉及金额，用于页面顶部的概览数字。"""
    items = collect(db, user, project_ids)
    return {
        "danger": sum(1 for i in items if i["level"] == "danger"),
        "warning": sum(1 for i in items if i["level"] == "warning"),
        "info": sum(1 for i in items if i["level"] == "info"),
        "total": len(items),
        "amount": sum(abs(i["amount"] or 0) for i in items if i["level"] == "danger"),
    }