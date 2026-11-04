"""全局提醒 API：聚合收款、工期、库存待办提醒，供顶部铃铛轮询。"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import (Material, Payment, Project, PurchaseContract, SubContract,
                      SupplierPayment, User)
from ..permissions import allowed_modules
from ..voided import not_voided

router = APIRouter(tags=["api"])


@router.get("/api/alerts")
def alerts_api(request: Request, db: Session = Depends(get_db),
               user: User | None = Depends(get_current_user)):
    """待办提醒。按当前用户的模块权限过滤，避免财务提醒/超付信息泄露给无关岗位。"""
    if user is None:
        return JSONResponse({"count": 0, "items": []})

    allowed = allowed_modules(user, db)
    today = date.today()
    items = []

    # 收款提醒：计划日已到或 3 天内到期，且未付清
    if "projects" in allowed:
        for p in db.query(Payment).all():
            if p.plan_date and p.balance > 0 and p.plan_date <= today + timedelta(days=3):
                items.append({
                    "type": "收款",
                    "text": f"{p.project.name} · {p.name} 应收 ¥{p.balance:,.0f}",
                    "url": f"/projects/{p.project_id}#payments",
                    "date": f"计划 {p.plan_date}",
                    "badge": "danger" if p.plan_date < today else "warning",
                })

    # 工期提醒：15 天内到期或已超期，且未验收/售后
    if "projects" in allowed:
        projects = db.query(Project).filter(
            Project.end_date.isnot(None),
            Project.status.notin_(["已验收", "售后"]),
        ).all()
        for p in projects:
            if p.end_date and p.end_date <= today + timedelta(days=15):
                items.append({
                    "type": "工期",
                    "text": f"{p.name}（{p.status}）",
                    "url": f"/projects/{p.id}",
                    "date": f"计划竣工 {p.end_date}",
                    "badge": "danger" if p.end_date < today else "warning",
                })

    # 库存提醒
    if "inventory" in allowed or "materials" in allowed:
        for m in db.query(Material).all():
            if m.low_stock_flag:
                items.append({
                    "type": "库存",
                    "text": f"{m.name} 库存仅剩 {m.stock} {m.unit}",
                    "url": "/inventory",
                    "date": f"预警线 {m.low_stock}",
                    "badge": "danger",
                })

    # 超付提醒：采购合同 / 分包合同 已付 > 合同额（涉资金，只给采招/财务岗位）
    if "supplier_payments" in allowed or "procurement" in allowed or "subcontracts" in allowed:
        _pc_paid = dict(db.query(SupplierPayment.contract_id,
                                 func.coalesce(func.sum(SupplierPayment.amount), 0.0)).filter(
            SupplierPayment.contract_id.isnot(None), not_voided(SupplierPayment)
        ).group_by(SupplierPayment.contract_id).all())
        for pc in db.query(PurchaseContract).all():
            if pc.amount and float(_pc_paid.get(pc.id) or 0) > pc.amount:
                _over = float(_pc_paid.get(pc.id) or 0) - pc.amount
                items.append({
                    "type": "超付",
                    "text": f"采购合同 {pc.name} 超付 ¥{_over:,.0f}",
                    "url": f"/contracts/{pc.id}",
                    "date": f"合同额 ¥{pc.amount:,.0f}",
                    "badge": "danger",
                })
        _sc_paid = dict(db.query(SupplierPayment.subcontract_id,
                                 func.coalesce(func.sum(SupplierPayment.amount), 0.0)).filter(
            SupplierPayment.subcontract_id.isnot(None), not_voided(SupplierPayment)
        ).group_by(SupplierPayment.subcontract_id).all())
        for sc in db.query(SubContract).all():
            if sc.amount and float(_sc_paid.get(sc.id) or 0) > sc.amount:
                _over = float(_sc_paid.get(sc.id) or 0) - sc.amount
                items.append({
                    "type": "超付",
                    "text": f"分包合同 {sc.name} 超付 ¥{_over:,.0f}",
                    "url": f"/subcontracts/{sc.id}",
                    "date": f"合同额 ¥{sc.amount:,.0f}",
                    "badge": "danger",
                })

    items.sort(key=lambda x: 0 if x["badge"] == "danger" else 1)
    return JSONResponse({"count": len(items), "items": items[:30]})
