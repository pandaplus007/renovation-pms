"""销项开票申请路由。

现状：`invoices` 表 41 张全是进项，0 张销项关联项目。收了 1376 万却说不清
开了多少票出去。这个模块把「向客户开多少税」变成有状态的申请单，
并把「已收 vs 已开 vs 待开」三个数并排放。
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import (INVOICE_APPLICATION_STATUS, INVOICE_TYPES, Customer,
                      InvoiceApplication, Payment, Project, User)
from ..sales_invoices import (create_invoice_for_app, list_applications,
                              over_invoiced_warnings, received_vs_invoiced,
                              status_counts)
from ..utils.audit import log_audit
from ..utils.excel_export import export_xlsx
from ..web import render

router = APIRouter(prefix="/sales-invoices", tags=["sales_invoices"])


def _f(v: str | None, d=0.0) -> float:
    try:
        return float(v) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _d(v: str | None):
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return None


@router.get("")
def sales_invoice_index(request: Request, status: str = "", project_id: int = 0,
                        db: Session = Depends(get_db),
                        user: User = Depends(login_required)):
    vs = received_vs_invoiced(db)
    rows = list_applications(db, status, project_id or None)
    counts = status_counts(db)
    return render(request, "sales_invoices/index.html", {
        "active": "sales_invoices", "user": user,
        "vs": vs, "rows": rows, "counts": counts, "status": status,
        "project_id": project_id,
        "statuses": INVOICE_APPLICATION_STATUS,
        "warnings": over_invoiced_warnings(db),
        "projects": db.query(Project).order_by(Project.name).all(),
    })


@router.get("/export")
def sales_invoice_export(request: Request, db: Session = Depends(get_db),
                         user: User = Depends(login_required)):
    rows = list_applications(db)
    data = []
    for r in rows:
        data.append([
            r["customer_name"], r["project_name"], r["amount"], r["tax_rate"],
            r["tax_excl"], r["tax_amount"], r["invoiced"], r["remain"],
            r["invoice_type"], r["title"], r["status"],
            r["apply_date"] or "", r["finish_date"] or "",
            (r["invoice"].invoice_no if r["invoice"] else ""), r["note"],
        ])
    vs = received_vs_invoiced(db)
    data.append([])
    data.append([f"已收款 {vs['total_received']:,.2f}　已开票 {vs['total_invoiced']:,.2f}"
                 f"　待开票 {vs['total_pending']:,.2f}　缺口 {vs['total_gap']:,.2f}"])
    return export_xlsx(
        "销项开票台账.xlsx",
        ["客户", "项目", "申请金额(含税)", "税率(%)", "不含税金额", "税额",
         "已开票", "待开票", "发票类型", "发票抬头", "状态",
         "申请日期", "开票完成日", "发票号码", "备注"],
        data, "销项开票",
    )


# ================================================================== 申请

@router.get("/new")
def sales_invoice_new(request: Request, project_id: int = 0, customer_id: int = 0,
                      payment_id: int = 0,
                      db: Session = Depends(get_db),
                      user: User = Depends(login_required)):
    p = db.get(Project, project_id) if project_id else None
    c = db.get(Customer, customer_id) if customer_id else (p.customer if p else None)
    return render(request, "sales_invoices/form.html", {
        "active": "sales_invoices", "user": user, "obj": None,
        "customers": db.query(Customer).order_by(Customer.name).all(),
        "projects": db.query(Project).order_by(Project.name).all(),
        "pre_customer": c, "pre_project": p, "pre_payment_id": payment_id,
        "statuses": INVOICE_APPLICATION_STATUS, "invoice_types": INVOICE_TYPES,
        "today": date.today(),
    })


@router.post("/save")
def sales_invoice_save(request: Request, app_id: int = Form(0),
                       customer_id: int = Form(0), project_id: int = Form(0),
                       payment_id: int = Form(0), title: str = Form(""),
                       tax_no: str = Form(""), invoice_type: str = Form("增值税普通发票"),
                       tax_rate: str = Form("0"), amount: str = Form("0"),
                       status: str = Form("待开票"), apply_date: str = Form(""),
                       note: str = Form(""),
                       db: Session = Depends(get_db),
                       user: User = Depends(login_required)):
    cid = int(customer_id or 0)
    if cid <= 0:
        return RedirectResponse("/sales-invoices/new", status_code=303)
    cust = db.get(Customer, cid)
    obj = db.get(InvoiceApplication, app_id) if app_id else None
    if obj is None:
        obj = InvoiceApplication(customer_id=cid)
        db.add(obj)
    obj.customer_id = cid
    obj.project_id = int(project_id) if project_id else None
    obj.payment_id = int(payment_id) if payment_id else None
    # 抬头与税号留空时回落到客户档案——开票信息客户建档时已录，
    # 每次手打一遍既慢又容易打错
    obj.title = title.strip() or (cust.name if cust else "")
    obj.tax_no = tax_no.strip() or (cust.tax_no if cust else "")
    obj.invoice_type = invoice_type or "增值税普通发票"
    obj.tax_rate = _f(tax_rate)
    obj.amount = abs(_f(amount))
    obj.status = status or "待开票"
    obj.apply_date = _d(apply_date) or date.today()
    obj.note = note.strip()
    db.commit()
    log_audit(db, user, "修改" if app_id else "新增", "开票申请", obj.id,
              f"{obj.title} {obj.amount:,.2f} {obj.status}")
    return RedirectResponse("/sales-invoices", status_code=303)


# ================================================================== 开票

@router.post("/{app_id}/issue")
def sales_invoice_issue(request: Request, app_id: int,
                        invoice_no: str = Form(""), invoice_date: str = Form(""),
                        amount: str = Form(""), file_path: str = Form(""),
                        db: Session = Depends(get_db),
                        user: User = Depends(login_required)):
    """从申请单开出销项发票。amount 留空或 0 = 开掉剩余全部。"""
    app = db.get(InvoiceApplication, app_id)
    if app is None:
        return RedirectResponse("/sales-invoices", status_code=303)
    inv = create_invoice_for_app(db, app, invoice_no.strip(),
                                 _d(invoice_date) or date.today(),
                                 _f(amount), file_path.strip())
    log_audit(db, user, "新增", "销项发票", inv.id,
              f"由申请单 #{app.id} 开出 {inv.amount:,.2f}，申请单现状态 {app.status}")
    return RedirectResponse("/sales-invoices", status_code=303)


@router.post("/{app_id}/status")
def sales_invoice_status(request: Request, app_id: int, status: str = Form(""),
                         db: Session = Depends(get_db),
                         user: User = Depends(login_required)):
    app = db.get(InvoiceApplication, app_id)
    if app is None or status not in INVOICE_APPLICATION_STATUS:
        return RedirectResponse("/sales-invoices", status_code=303)
    if status == "已作废":
        # 作废时把已开票额清零：这张申请单不该再计入「已开票」统计。
        # 不清零的话，对照表会同时算上作废票的金额与它开出的发票，两头都错。
        app.invoiced_amount = 0
    app.status = status
    db.commit()
    log_audit(db, user, "修改", "开票申请", app_id, f"状态 -> {status}")
    return RedirectResponse("/sales-invoices", status_code=303)


@router.post("/{app_id}/delete")
def sales_invoice_delete(request: Request, app_id: int, db: Session = Depends(get_db),
                         user: User = Depends(login_required)):
    app = db.get(InvoiceApplication, app_id)
    if app is None:
        return RedirectResponse("/sales-invoices", status_code=303)
    info = f"{app.title} {app.amount:,.2f}"
    db.delete(app)
    db.commit()
    log_audit(db, user, "删除", "开票申请", app_id, info)
    return RedirectResponse("/sales-invoices", status_code=303)