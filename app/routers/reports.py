"""成本与经营报表。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import login_required
from ..approvals import filter_booked
from ..database import get_db
from .. import changes
from ..models import (Contract, CostItem, Payment, Project, PurchaseContract,
                      PurchaseOrder, PurchaseOrderItem, SubContract, SubOrder,
                      User)
from ..utils.excel_export import export_xlsx
from ..row_scope import can_see_project, project_scope_filter, visible_projects
from ..voided import VOID_ATTRS, filter_active, not_voided
from ..web import render

router = APIRouter(prefix="/reports", tags=["reports"])


def _cost_basis(db: Session):
    """项目成本的三种口径，一次算好，供毛利报表的「口径对照」用。

    为什么需要三个口径
    ------------------
    装修项目的成本有个特点：**合同签了不等于钱花完了**。采购合同签了 385 万、
    分包合同签了 1125 万，但实际下单只有一部分。单看「已下单成本」会把
    还没下单的钱排除在毛利之外，毛利率会明显偏高；单看「合同全额成本」
    又会把可能永不发生的部分算成亏损。两个数都得看，才知道真实区间。

        ordered   已下单  = 采购单明细金额 + 分包单金额   ← 当前毛利报表用的
        committed 已签约  = 采购合同额 + 分包合同额       ← 最保守，毛利下限
        未下单    = committed − ordered                 ← 两个口径的差
    """
    ordered_pur = dict(db.query(
        PurchaseOrder.project_id,
        func.coalesce(func.sum(PurchaseOrderItem.quantity * PurchaseOrderItem.unit_price), 0.0),
    ).join(PurchaseOrderItem, PurchaseOrderItem.po_id == PurchaseOrder.id)
        .filter(PurchaseOrder.project_id.isnot(None))
        .group_by(PurchaseOrder.project_id).all())
    ordered_sub = dict(db.query(
        SubContract.project_id,
        func.coalesce(func.sum(SubOrder.total_amount), 0.0),
    ).join(SubOrder, SubOrder.subcontract_id == SubContract.id)
        .filter(SubContract.project_id.isnot(None))
        .group_by(SubContract.project_id).all())
    commit_pur = dict(db.query(
        PurchaseContract.project_id, func.coalesce(func.sum(PurchaseContract.amount), 0.0),
    ).filter(PurchaseContract.project_id.isnot(None))
        .group_by(PurchaseContract.project_id).all())
    commit_sub = dict(db.query(
        SubContract.project_id, func.coalesce(func.sum(SubContract.amount), 0.0),
    ).filter(SubContract.project_id.isnot(None))
        .group_by(SubContract.project_id).all())
    return ordered_pur, ordered_sub, commit_pur, commit_sub


def _profit_data(db: Session, user=None):
    """项目毛利：三种成本口径对照。

    收入 = 施工合同额 + 已确认变更（不含未签字的变更）
    成本 = 三档：
      已下单（当前口径，最常用）
      已签约（合同全额，毛利下限——未下单的合同款也可能真要花）
      两者之差（尚未下单的敞口）

    传 `user` 时按其数据范围过滤项目（行级权限，见 app/row_scope.py）。
    ⚠ **必须传**：这里返回的是合同额、成本、毛利——全系统最敏感的一组数。
    2026-10-13 修复：本函数原先没有 `user`，`db.query(Project)` 直接全量，
    于是 `/reports/profit` 与 `/reports/profit/export` 对所有有 reports
    查看权的岗位展示的是全公司口径。`LEGACY_ROLE_MODULES` 里
    工程部/财务部/采购部/行政部**默认都有 `reports`**，
    所以这不是「理论上可能」，是默认配置下就在泄露。

    为什么这一处曾经是漏的：同文件的 `_cost_data(db, user)` 从一开始就
    带过滤（它服务 costs_export，作者记得），而 `_profit_data` 是后加的，
    照着调用处 `rows, totals = _profit_data(db)` 抄了个签名就完事。
    这类「同一个概念两处实现，其中一处忘了加固」的问题肉眼与 GET 测试
    都发现不了：页面照常渲染，只是数字多了。

    不传 user = 全量，保留给 CLI / 推送等「老板视角」的调用方。
    """
    cond = project_scope_filter(user, Project) if user is not None else None
    q = db.query(Project)
    if cond is not None:
        q = q.filter(cond)
    projects = q.order_by(Project.id).all()
    _pids = [p.id for p in projects]
    # 合同（project_id 唯一）
    contracts = {c.project_id: c for c in db.query(Contract).all() if c.project_id}
    contract_map = {pid: c.amount for pid, c in contracts.items()}
    # 已收款（排除作废节点）
    received_map = dict(db.query(Payment.project_id, func.coalesce(func.sum(Payment.paid_amount), 0.0))
                        .filter(not_voided(Payment, VOID_ATTRS["payments"]))
                        .group_by(Payment.project_id).all()) if _pids else {}
    # 成本三档
    ordered_pur, ordered_sub, commit_pur, commit_sub = _cost_basis(db)
    # 费用：按项目聚合。未审批不入账开启时，未获审批通过的费用不计入成本。
    # 原本是 SQL 聚合，改为内存聚合以便逐笔过滤（数据量是单机项目级，可接受）。
    cost_map: dict = {}
    for c in filter_booked(db, "cost_item", db.query(CostItem).filter(not_voided(CostItem)).all()):
        pid = c.project_id
        cost_map[pid] = cost_map.get(pid, 0.0) + (c.amount or 0.0)
    # 已确认变更（收入侧）
    chg_map = changes.project_change_totals(db)

    rows = []
    acc = {k: 0.0 for k in (
        "contract", "revenue", "change", "received", "purchase", "subcontract", "cost",
        "ordered_cost", "committed_cost", "uncommitted", "profit", "profit_low",
        "settle", "change_cost", "change_profit", "pending_change")}
    for p in projects:
        ct = contracts.get(p.id)
        contract_amt = float(contract_map.get(p.id) or 0)
        chg = chg_map.get(p.id) or {}
        change_confirmed = float(chg.get("confirmed", 0) or 0)
        change_cost = float(chg.get("cost", 0) or 0)
        change_pending = float(chg.get("pending_amount", 0) or 0)
        # 收入：合同额 + 已确认变更。分界线是**业主有没有签字**，
        # 不是流程走到哪一步——「已定价待业主审」已报出去等签，在账上仍是零。
        revenue = round(contract_amt + change_confirmed, 2)

        received = float(received_map.get(p.id) or 0)
        purchase = float(ordered_pur.get(p.id) or 0)
        subcontract = float(ordered_sub.get(p.id) or 0)
        cost = float(cost_map.get(p.id) or 0)
        ordered_cost = purchase + subcontract + cost
        # 已签约成本：采购合同全额 + 分包合同全额 + 费用
        committed_cost = float(commit_pur.get(p.id) or 0) + float(commit_sub.get(p.id) or 0) + cost
        uncommitted = committed_cost - ordered_cost

        profit = revenue - ordered_cost
        profit_low = revenue - committed_cost       # 毛利下限（按已签约算成本）
        settle = changes.settle_amount(db, p.id, ct)

        for k, v in (("contract", contract_amt), ("revenue", revenue),
                     ("change", change_confirmed),
                     ("received", received), ("purchase", purchase),
                     ("subcontract", subcontract), ("cost", cost),
                     ("ordered_cost", ordered_cost), ("committed_cost", committed_cost),
                     ("uncommitted", uncommitted), ("profit", profit),
                     ("profit_low", profit_low), ("settle", settle),
                     ("change_cost", change_cost),
                     ("change_profit", round(change_confirmed - change_cost, 2)),
                     ("pending_change", change_pending)):
            acc[k] += v

        rows.append({
            "project": p, "contract": round(contract_amt, 2),
            "revenue": revenue, "change": round(change_confirmed, 2),
            "change_cost": round(change_cost, 2),
            "change_profit": round(change_confirmed - change_cost, 2),
            "pending_change": round(change_pending, 2),
            "settle": settle,
            "received": round(received, 2),
            "purchase": round(purchase, 2), "subcontract": round(subcontract, 2),
            "cost": round(cost, 2),
            "ordered_cost": round(ordered_cost, 2),
            "committed_cost": round(committed_cost, 2),
            "uncommitted": round(uncommitted, 2),
            "profit": round(profit, 2),
            "profit_rate": round(profit / revenue * 100, 1) if revenue else 0,
            "profit_low": round(profit_low, 2),
            "profit_rate_low": round(profit_low / revenue * 100, 1) if revenue else 0,
            # 毛利率是否已低于装修行业常见下限（低于 20% 值得复盘）
            "thin": bool(revenue and profit_rate_low(revenue, profit_low) < 20),
        })

    # 注意 acc 里必须含 "contract"（合同额合计）——模板的合计行用到它，
    # 而 acc 是由各行的同名键累加来的，漏掉就是 totals.contract 为 None。
    totals_out = {k: round(v, 2) for k, v in acc.items()}
    totals_out["profit_rate"] = (round(acc["profit"] / acc["revenue"] * 100, 1)
                                 if acc["revenue"] else 0)
    totals_out["profit_rate_low"] = (round(acc["profit_low"] / acc["revenue"] * 100, 1)
                                      if acc["revenue"] else 0)
    return rows, totals_out


def profit_rate_low(revenue: float, profit_low: float) -> float:
    return (profit_low / revenue * 100) if revenue else 0


@router.get("/profit")
def profit_report(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(login_required)):
    rows, totals = _profit_data(db, user)
    return render(request, "reports/profit.html", {
        "active": "reports", "user": user, "rows": rows, "totals": totals,
    })


@router.get("/profit/export")
def profit_report_export(request: Request, db: Session = Depends(get_db),
                         user: User = Depends(login_required)):
    # 导出按当前用户的数据范围过滤——导出文件会离开系统，
    # 权限只在页面上做而导出全量，等于把不该看的金额发出去。
    # （与 /costs/export 同一理由，见那处的注释。）
    rows, totals = _profit_data(db, user)
    data = [[r["project"].name, r["project"].status, r["contract"], r["change"],
             r["revenue"], r["received"], r["purchase"], r["subcontract"], r["cost"],
             r["ordered_cost"], r["committed_cost"], r["uncommitted"],
             r["profit"], r["profit_rate"], r["profit_low"], r["profit_rate_low"]]
            for r in rows]
    data.append([])
    data.append(["合计", "", totals["contract"], totals["change"],
                 totals["revenue"], totals["received"], totals["purchase"],
                 totals["subcontract"], totals["cost"], totals["ordered_cost"],
                 totals["committed_cost"], totals["uncommitted"],
                 totals["profit"], totals["profit_rate"],
                 totals["profit_low"], totals["profit_rate_low"]])
    return export_xlsx(
        "项目毛利报表.xlsx",
        ["项目", "状态", "施工合同额", "已确认变更", "收入合计", "已收款",
         "采购成本(已下单)", "分包成本(已下单)", "费用",
         "成本-已下单口径", "成本-已签约口径", "未下单敞口",
         "毛利(已下单口径)", "毛利率(%)",
         "毛利下限(已签约口径)", "毛利率下限(%)"],
        data, "各项目经营情况",
    )


def _ledger_overview(db: Session, user=None):
    """所有项目的资金台账汇总（复用项目台账逻辑）。

    ⚠ 2026-10-13：**当前没有任何调用方**（全项目 grep 只有这一个定义）。
    保留是因为它有参考价值——它是唯一一处把各项目台账汇总成一张表的实现。

    但正因为没人调用，它过去带不带行级过滤都看不出来，所以**不要**把它
    当成「已经安全」的样板：它原先也是 `db.query(Project)` 全量。
    现在按 `_profit_data` / `_cost_data` 的同一写法加上了 `user`，
    以免将来有人接上去时又把一个未过滤的口子带回来。
    """
    from .projects import _ledger_data
    cond = project_scope_filter(user, Project) if user is not None else None
    q = db.query(Project)
    if cond is not None:
        q = q.filter(cond)
    projects = q.order_by(Project.id).all()
    rows = []
    totals = {"received": 0.0, "material": 0.0, "labor": 0.0, "cost": 0.0,
              "tax": 0.0, "balance": 0.0, "profit": 0.0}
    for p in projects:
        ld = _ledger_data(db, p.id)
        s = ld["summary"]
        rows.append({"project": p, "summary": s, "inv_totals": ld["inv_totals"],
                     "invoice_out_total": ld["invoice_out_total"]})
        for k in totals:
            totals[k] = round(totals[k] + s.get(k, 0), 2)
    return rows, totals


@router.get("/ledger")
def ledger_report(request: Request, project_id: int = 0,
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    """项目资金台账（完整页面，带项目切换）。"""
    from .projects import LEDGER_CATEGORIES, _ledger_data
    # 项目切换下拉按数据范围过滤（行级权限）。原先这里是全量，
    # 收紧范围的用户在**这个下拉里就能看到全部项目名**，
    # 而点进去才是别人的台账 —— 等于「看不见数据但看得见有哪些项目」。
    projects = visible_projects(db, user, Project)
    projects = sorted(projects, key=lambda p: p.name or "")

    # ⚠ 二次校验必须排在「无可视项目」的提前返回**之前**。
    #   顺序反了的话，一个可见项目为 0 的用户访问
    #   `?project_id=<别人的 id>` 会拿到 200 的空白页而不是 404 ——
    #   虽然页面上没有任何数据（不算泄露），但「404 不泄露项目是否存在」
    #   这条纪律就破了：同一个地址在有权限时 200、无权限时 200，
    #   差别只在内容，探测者仍然能靠响应体长度做二分。
    project = None
    if project_id:
        # 不能只靠下拉过滤——URL 里的 ?project_id= 是可以手输的，
        # 历史书签、企微推送里的链接都会绕过下拉。
        # 返回 404 而不是 403：403 泄露「这个项目存在」，404 不泄露任何信息。
        cand = db.get(Project, project_id)
        if cand is None or not can_see_project(user, cand, Project):
            raise HTTPException(status_code=404, detail="项目不存在")
        project = cand

    if project is None:
        if not projects:
            return render(request, "reports/ledger.html", {
                "active": "reports", "user": user, "projects": [], "project": None,
                "ledger": None, "ledger_categories": LEDGER_CATEGORIES,
                "contract_total": 0, "today": date.today(),
            })
        project = projects[0]
    ledger = _ledger_data(db, project.id)
    _ct = project.contract
    contract_total = _ct.amount if _ct else 0
    return render(request, "reports/ledger.html", {
        "active": "reports", "user": user, "projects": projects, "project": project,
        "ledger": ledger, "ledger_categories": LEDGER_CATEGORIES,
        "contract_total": contract_total, "today": date.today(),
    })


def _cost_data(db: Session, user=None):
    """计算各项目经营数据，供页面与导出共用（兼容旧导出）。
    成本合计 = 分包采购单 + 采购单 + 费用；毛利 = 结算金额（无则合同额）− 成本。

    传 `user` 时按其数据范围过滤项目（行级权限，见 app/row_scope.py）。
    ⚠ **必须过滤**：这里返回的是合同额、成本、毛利——最敏感的数字。
    页面只显示了某人可见的项目、汇总却含全部，就是「看起来做了权限、
    实际没做」，比不做更危险。
    不传 user 时行为与从前完全一致（导出等旧调用方）。
    """
    if user is None:
        projects = db.query(Project).all()
    else:
        cond = project_scope_filter(user, Project)
        q = db.query(Project)
        if cond is not None:
            q = q.filter(cond)
        projects = q.all()
    rows = []
    total_contract = total_received = total_cost = total_settle = 0.0
    for p in projects:
        contract = p.contract.amount if p.contract else 0
        settle = p.contract.settle_amount if p.contract else 0
        received = sum(pay.paid_amount for pay in p.payments
                       if getattr(pay, "void_status", "正常") != "作废")
        # 费用同样受「未审批不入账」与「已作废」两个约束，
        # 保证与项目详情页台账口径一致。
        # ⚠ 两个是**并列**条件不是替代关系：filter_booked 只管审批，
        #   不过滤作废；少写 filter_active 会让作废的费用继续进成本。
        cost = sum(c.amount for c in
                   filter_active(filter_booked(db, "cost_item", p.cost_items)))
        cost += sum(po.total_amount for po in db.query(PurchaseOrder).filter(
            PurchaseOrder.project_id == p.id).all())
        cost += sum(o.total_amount for sc in db.query(SubContract).filter(
            SubContract.project_id == p.id).all() for o in sc.orders)
        budget = sum(b.budget_amount for b in p.budget_items)
        income = settle if settle else contract          # 结算金额优先作为收入口径
        profit = income - cost
        total_contract += contract
        total_received += received
        total_cost += cost
        total_settle += settle
        rows.append({
            "project": p, "contract": contract, "received": received, "settle": settle,
            "cost": cost, "budget": budget, "profit": profit,
            "profit_rate": (profit / income * 100) if income else 0,
            "over": budget > 0 and cost > budget,
        })
    profit_total = total_settle - total_cost
    profit_rate = (profit_total / (total_settle or total_contract) * 100) if (total_settle or total_contract) else 0
    return rows, total_contract, total_received, total_cost, profit_total, profit_rate


@router.get("/costs/export")
def cost_report_export(request: Request, db: Session = Depends(get_db),
                       user: User = Depends(login_required)):
    # 导出按当前用户的数据范围过滤——导出文件会离开系统，
    # 权限只在页面上做而导出全量，等于把不该看的金额发出去。
    rows, total_contract, total_received, total_cost, profit_total, profit_rate = _cost_data(db, user)
    data = []
    for r in rows:
        data.append([
            r["project"].name, r["project"].status, r["contract"], r["received"],
            r["cost"], r["budget"], r["profit"], round(r["profit_rate"], 2),
        ])
    data.append([])
    data.append(["合计", "", total_contract, total_received, total_cost, "",
                 profit_total, round(profit_rate, 2)])
    return export_xlsx(
        "成本报表.xlsx",
        ["项目", "状态", "合同额", "已收款", "成本合计", "预算合计", "毛利", "毛利率(%)"],
        data, "各项目经营情况",
    )
