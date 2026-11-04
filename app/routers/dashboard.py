"""项目总览（Dashboard）：核心统计与预警。

⚠ 行级权限（app/row_scope.py）：本模块的所有统计与列表**必须**按当前用户的
可见项目过滤。这里是登录后的第一屏，也是最容易泄露的地方——合同额合计、
应收余额、项目名都从这里出。老板看到全量，员工只看到自己的。

改这份文件时注意：任何新增的 `db.query(Project)` 都要套上范围过滤。
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import Contract, Customer, Material, Payment, Project, User
from ..row_scope import visible_projects, user_scope
from ..web import render

router = APIRouter(tags=["dashboard"])


@router.get("/todo")
def todo_board(request: Request, db: Session = Depends(get_db),
               user: User = Depends(login_required)):
    """待办总览：把散在各模块的异常集中到一屏，每条可直达处理。

    与 /api/alerts 的分工：那个只管单据级提醒（收款/工期/库存/超付），
    这里管更宽的经营异常（超付、送货缺口、分包缺口、成本超预算、客户跟进）。
    """
    from ..approvals import pending_for_role
    from ..todo import collect, summary
    # 传 user：待办里带着项目名、客户名与欠款金额，是最敏感的一屏。
    # 不传的话，收紧范围的员工照样在这里看到全公司的账。
    items = collect(db, user)
    approvals = pending_for_role(db, user)
    return render(request, "todo.html", {
        "active": "todo", "user": user,
        "items": items, "stat": summary(db, user), "approvals": approvals,
        "empty": not items and not approvals,
        "data_scope": user_scope(user),
    })


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db),
              user: User = Depends(login_required)):
    today = date.today()

    # 先算出可见项目，后面每处统计都基于它。
    # 反过来做（各处单独过滤）最容易漏——本文件历史上就有 5 处独立查询。
    _vis = visible_projects(db, user, Project)
    _vis_ids = [p.id for p in _vis]
    _vis_set = set(_vis_ids)

    # ---- 统计卡片 ----
    stats = {
        "building": sum(1 for p in _vis if p.status == "施工中"),
        "pending": sum(1 for p in _vis if p.status in ("待签约", "待开工")),
        # 客户数按「有可见项目关联」算，而不是全部客户——
        # 否则「客户 128 家」这个数字本身就在泄露公司规模与历史。
        "customers": len({p.customer_id for p in _vis if p.customer_id}),
        "contract_total": sum((c.amount or 0) for c in db.query(Contract).filter(
            Contract.project_id.in_(_vis_ids)).all()) if _vis_ids else 0,
        # 应收余额：已排除作废节点（收款节点作废 = 这笔钱不算数）
        "receivable": sum(p.balance for p in db.query(Payment).filter(
            Payment.project_id.in_(_vis_ids),
            getattr(Payment, "void_status") != "作废").all()) if _vis_ids else 0,
        "month_contract": sum(1 for c in db.query(Contract).filter(
            Contract.project_id.in_(_vis_ids),
            func.strftime("%Y-%m", Contract.sign_date) == today.strftime("%Y-%m")).all()
        ) if _vis_ids else 0,
    }

    # ---- 收款预警：计划日已到 / 即将到期 且未付清 ----
    # Payment.void_status：作废的节点不该出现在「该催款了」里。
    payments = db.query(Payment).filter(
        Payment.project_id.in_(_vis_ids),
        getattr(Payment, "void_status") != "作废").all() if _vis_ids else []
    payment_alerts = []
    for p in payments:
        if p.plan_date and p.balance > 0 and p.plan_date <= today + timedelta(days=7):
            payment_alerts.append(p)
    payment_alerts.sort(key=lambda p: p.plan_date)

    # ---- 工期预警：15 天内到期或已超期，未验收 ----
    deadline_alerts = [p for p in _vis
                       if p.end_date and p.status not in ("已验收", "售后")
                       and p.end_date <= today + timedelta(days=15)]

    # ---- 库存预警：库存与材料是**全局**资源，不按项目分。
    # 有意不过滤：低库存提醒对每个有采购权限的人都是有用的。
    # 这一条是刻意的例外，不是遗漏——材料没有「属于某个项目」的概念。
    low_stock_materials = [m for m in db.query(Material).all() if m.low_stock_flag]

    # ---- 成本超支项目（实际成本 > 预算合计）----
    from ..approvals import filter_booked
    from ..voided import filter_active
    cost_over_projects = []
    for p in _vis:
        budget_total = sum(b.budget_amount for b in p.budget_items)
        # 与项目台账同口径：排除「未审批通过」与「已作废」两类费用，
        # 否则这里报出的超支数字会和项目详情页对不上。
        cost_total = sum(c.amount for c in
                         filter_active(filter_booked(db, "cost_item", p.cost_items)))
        if budget_total > 0 and cost_total > budget_total:
            cost_over_projects.append((p, budget_total, cost_total))

    # ---- 最近项目 ----
    recent_projects = sorted(_vis, key=lambda p: p.updated_at or today,
                             reverse=True)[:6]

    return render(request, "dashboard.html", {
        "active": "dashboard", "user": user, "stats": stats,
        "payment_alerts": payment_alerts, "deadline_alerts": deadline_alerts,
        "low_stock_materials": low_stock_materials,
        "cost_over_projects": cost_over_projects,
        "recent_projects": recent_projects,
        "today": today,
        "data_scope": user_scope(user),
        "vis_count": len(_vis),
    })
