"""分包管理：分包合同 + 分包采购单。功能参考采购管理。"""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import (Invoice, Project, SubContract, SubContractMaterial,
                      SubContractMilestone, SubOrder, Supplier, SupplierPayment, User)
from ..utils.audit import log_audit
from ..voided import not_voided
from ..web import render

router = APIRouter(prefix="/subcontracts", tags=["subcontracts"])

CATEGORIES = ["劳务分包", "专业分包", "机械租赁", "其他"]


def _date(v: str):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _f(v):
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


@router.get("")
def subcontract_list(request: Request, q: str = "", project_id: list[int] = Query([]),
                     category: list[str] = Query([]),
                     db: Session = Depends(get_db),
                     user: User = Depends(login_required)):
    cq = db.query(SubContract)
    if q:
        like = f"%{q}%"
        cq = cq.filter((SubContract.name.like(like))
                       | (SubContract.contract_no.like(like))
                       | (SubContract.party_b.like(like)))
    if category:
        cq = cq.filter(SubContract.category.in_(category))
    if -1 in project_id:
        _others = [x for x in project_id if x != -1]
        if _others:
            cq = cq.filter((SubContract.project_id.is_(None)) | (SubContract.project_id.in_(_others)))
        else:
            cq = cq.filter(SubContract.project_id.is_(None))
    elif project_id:
        cq = cq.filter(SubContract.project_id.in_(project_id))
    contracts = cq.order_by(SubContract.contract_no.desc(), SubContract.id.desc()).all()
    # 已付款按分包合同一次性聚合，避免 N+1
    _scids = [c.id for c in contracts]
    _pay_map = dict(db.query(SupplierPayment.subcontract_id,
                             func.coalesce(func.sum(SupplierPayment.amount), 0.0)).filter(
        SupplierPayment.subcontract_id.in_(_scids), not_voided(SupplierPayment)).group_by(
        SupplierPayment.subcontract_id).all()) if _scids else {}
    data = []
    for c in contracts:
        ordered = sum(o.total_amount for o in c.orders)
        remain = round(c.amount - ordered, 2) if c.amount else 0
        paid = round(float(_pay_map.get(c.id) or 0), 2)
        remain_pay = max(round(c.amount - paid, 2), 0) if c.amount else 0
        progress = round(ordered / c.amount * 100, 1) if c.amount else 0
        data.append({"contract": c, "ordered": ordered, "remain": remain, "remain_pay": remain_pay,
                     "paid": paid, "progress": progress,
                     "amount_pct": 100.0,
                     "ordered_pct": progress,
                     "remain_pct": round(max(remain, 0) / c.amount * 100, 1) if c.amount else 0,
                     "paid_pct": round(paid / c.amount * 100, 1) if c.amount else 0,
                     "remain_pay_pct": round(remain_pay / c.amount * 100, 1) if c.amount else 0})
    projects = db.query(Project).order_by(Project.name).all()
    all_cats = list(CATEGORIES)
    for (cat,) in db.query(SubContract.category).filter(SubContract.category != "").distinct().all():
        if cat not in all_cats:
            all_cats.append(cat)
    response = render(request, "subcontracts/list.html", {
        "active": "subcontracts", "user": user, "data": data,
        "projects": projects, "categories": all_cats,
        "q": q, "project_id": project_id, "category": category,
    })
    response.headers["Cache-Control"] = "no-store"
    return response



@router.get("/new")
def subcontract_new(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(login_required)):
    projects = db.query(Project).order_by(Project.name).all()
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    response = render(request, "subcontracts/form.html", {
        "active": "subcontracts", "user": user, "projects": projects,
        "categories": CATEGORIES, "parsed": None, "error": "", "suppliers": suppliers,
        # 新建时没有实体，但**必须显式传 None**：form.html 里 20 多处用的是
        # `sc.x if sc else ...`，Undefined 走 falsy 分支不会出事，唯独
        # action 那行是裸 `sc.id` ——对 Undefined 取属性会抛 UndefinedError，
        # 整个新建页 500。漏传这个键的表现是「新建分包合同打不开」，
        # 而编辑页正常，所以很难联想到是同一个模板的问题。
        "sc": None,
    })
    response.headers["Cache-Control"] = "no-store"
    return response



@router.get("/{subcontract_id}/edit")
def subcontract_edit(subcontract_id: int, request: Request,
                     db: Session = Depends(get_db),
                     user: User = Depends(login_required)):
    sc = db.get(SubContract, subcontract_id)
    if sc is None:
        return RedirectResponse(url="/subcontracts", status_code=303)
    projects = db.query(Project).order_by(Project.name).all()
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    return render(request, "subcontracts/form.html", {
        "active": "subcontracts", "user": user, "projects": projects,
        "categories": CATEGORIES, "parsed": None, "error": "", "suppliers": suppliers,
        "sc": sc,
        # 审批留痕：编辑已有分包合同时带上单据 id
        "approval_entity": "sub_contract",
        "approval_id": sc.id,
    })


@router.post("/{subcontract_id}/edit")
def subcontract_update(subcontract_id: int, request: Request,
                       contract_no: str = Form(""), name: str = Form(...),
                       project_id: int = Form(0), party_b: str = Form(""),
                       amount: float = Form(0), category: str = Form("劳务分包"),
                       sign_date: str = Form(""), note: str = Form(""),
                       supplier_name: str = Form(""), party_a: str = Form(""),
                       a_tax_no: str = Form(""), a_address: str = Form(""),
                       a_contact: str = Form(""), a_bank: str = Form(""), a_account: str = Form(""),
                       b_tax_no: str = Form(""), b_bank: str = Form(""), b_account: str = Form(""),
                       duration_days: int = Form(0),
                       db: Session = Depends(get_db), user: User = Depends(login_required)):
    sc = db.get(SubContract, subcontract_id)
    if sc is None:
        return RedirectResponse(url="/subcontracts", status_code=303)
    if not party_b and supplier_name:
        party_b = supplier_name.strip()
    sc.contract_no = contract_no
    sc.name = name
    sc.project_id = project_id or None
    sc.party_b = party_b
    sc.party_a = party_a
    sc.a_tax_no = a_tax_no
    sc.a_address = a_address
    sc.a_contact = a_contact
    sc.a_bank = a_bank
    sc.a_account = a_account
    sc.b_tax_no = b_tax_no
    sc.b_bank = b_bank
    sc.b_account = b_account
    sc.duration_days = duration_days
    sc.amount = amount
    sc.category = category
    sc.sign_date = _date(sign_date)
    sc.note = note
    db.commit()
    log_audit(db, user, "修改", "分包合同", sc.id, f"{sc.name} {sc.contract_no or ''}".strip())
    return RedirectResponse(url=f"/subcontracts/{subcontract_id}", status_code=303)


@router.post("/parse")
async def subcontract_parse(request: Request, file: UploadFile = File(...),
                            db: Session = Depends(get_db),
                            user: User = Depends(login_required)):
    """上传分包合同 → 识别 → 预填表单。"""
    projects = db.query(Project).order_by(Project.name).all()
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    ctx = {"active": "subcontracts", "user": user, "projects": projects,
           "categories": CATEGORIES, "parsed": None, "error": "", "suppliers": suppliers}
    if not file or not file.filename:
        ctx["error"] = "请选择分包合同文件"
        _resp = render(request, "subcontracts/form.html", ctx)
        _resp.headers["Cache-Control"] = "no-store"
        return _resp

    data = await file.read()
    from ..utils.contract_parser import recognize_contract_file
    parsed, parse_err = recognize_contract_file(file.filename, data)
    if parsed:
        ctx["parsed"] = parsed
    else:
        ctx["error"] = parse_err or "未能识别该合同（可能是扫描件），可手动填写"
    response = render(request, "subcontracts/form.html", ctx)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/orders/new")
def suborder_new(request: Request, subcontract_id: int = 0,
                 db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    subcontracts = db.query(SubContract).order_by(SubContract.contract_no.desc(),
                                                  SubContract.id.desc()).all()
    response = render(request, "subcontracts/order_form.html", {
        "active": "subcontracts", "user": user, "subcontracts": subcontracts,
        "prefill_subcontract_id": subcontract_id,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/orders/new")
def suborder_create(request: Request,
                    subcontract_id: int = Form(...), order_no: str = Form(""),
                    total_amount: float = Form(0), order_date: str = Form(""),
                    note: str = Form(""),
                    db: Session = Depends(get_db),
                    user: User = Depends(login_required)):
    sc = db.get(SubContract, subcontract_id)
    if sc:
        db.add(SubOrder(subcontract_id=subcontract_id, order_no=order_no,
                        total_amount=total_amount, order_date=_date(order_date), note=note))
        db.commit()
    return RedirectResponse(url=f"/subcontracts/{subcontract_id}", status_code=303)


@router.post("/new")
def subcontract_create(request: Request,
                       contract_no: str = Form(""), name: str = Form(...),
                       project_id: int = Form(0), party_b: str = Form(""),
                       amount: float = Form(0), category: str = Form("劳务分包"),
                       sign_date: str = Form(""), note: str = Form(""),
                       supplier_name: str = Form(""), party_a: str = Form(""),
                       a_tax_no: str = Form(""), a_address: str = Form(""),
                       a_contact: str = Form(""), a_bank: str = Form(""), a_account: str = Form(""),
                       b_tax_no: str = Form(""), b_bank: str = Form(""), b_account: str = Form(""),
                       duration_days: int = Form(0),
                       ms_name: list[str] = Form([]), ms_date: list[str] = Form([]),
                       ms_amount: list[str] = Form([]),
                       cm_name: list[str] = Form([]), cm_spec: list[str] = Form([]),
                       cm_unit: list[str] = Form([]), cm_quantity: list[str] = Form([]),
                       cm_unit_price: list[str] = Form([]), cm_amount: list[str] = Form([]),
                       db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not party_b and supplier_name:
        party_b = supplier_name.strip()
    sc = SubContract(contract_no=contract_no, name=name,
                     project_id=project_id or None, party_b=party_b,
                     party_a=party_a, a_tax_no=a_tax_no, a_address=a_address,
                     a_contact=a_contact, a_bank=a_bank, a_account=a_account,
                     b_tax_no=b_tax_no, b_bank=b_bank, b_account=b_account,
                     duration_days=duration_days,
                     amount=amount, category=category,
                     sign_date=_date(sign_date), note=note)
    db.add(sc)
    db.flush()
    for i, mn in enumerate(ms_name):
        mn = (mn or "").strip()
        if not mn:
            continue
        db.add(SubContractMilestone(subcontract_id=sc.id, name=mn,
                                    m_date=_date(ms_date[i]) if i < len(ms_date) else None,
                                    amount=float(ms_amount[i] or 0) if i < len(ms_amount) else 0))
    for i, cn in enumerate(cm_name):
        cn = (cn or "").strip()
        if not cn:
            continue
        qty = float(cm_quantity[i] or 0) if i < len(cm_quantity) else 0
        price = float(cm_unit_price[i] or 0) if i < len(cm_unit_price) else 0
        amt = float(cm_amount[i] or 0) if i < len(cm_amount) else round(qty * price, 2)
        db.add(SubContractMaterial(subcontract_id=sc.id, name=cn,
                                   spec=cm_spec[i] if i < len(cm_spec) else "",
                                   unit=cm_unit[i] if i < len(cm_unit) else "",
                                   quantity=qty, unit_price=price, amount=amt))
    db.commit()
    log_audit(db, user, "新增", "分包合同", sc.id, f"{sc.name} {sc.contract_no or ''}".strip())
    return RedirectResponse(url=f"/subcontracts/{sc.id}", status_code=303)


@router.post("/{subcontract_id}/delete")
def subcontract_delete(subcontract_id: int, request: Request,
                       db: Session = Depends(get_db),
                       user: User = Depends(login_required)):
    sc = db.get(SubContract, subcontract_id)
    if sc:
        _scname = f"{sc.name} {sc.contract_no or ''}".strip()
        db.delete(sc)
        db.commit()
        log_audit(db, user, "删除", "分包合同", subcontract_id, _scname)
    return RedirectResponse(url="/subcontracts", status_code=303)


@router.post("/{subcontract_id}/orders")
def suborder_create(subcontract_id: int, request: Request,
                    order_no: str = Form(""), total_amount: float = Form(0),
                    order_date: str = Form(""), note: str = Form(""),
                    db: Session = Depends(get_db),
                    user: User = Depends(login_required)):
    sc = db.get(SubContract, subcontract_id)
    if sc:
        db.add(SubOrder(subcontract_id=subcontract_id, order_no=order_no,
                        total_amount=total_amount, order_date=_date(order_date), note=note))
        db.commit()
    return RedirectResponse(url=f"/subcontracts/{subcontract_id}", status_code=303)


@router.post("/orders/{order_id}/edit")
def suborder_update(order_id: int, request: Request,
                    order_no: str = Form(""), total_amount: float = Form(0),
                    order_date: str = Form(""), note: str = Form(""),
                    db: Session = Depends(get_db),
                    user: User = Depends(login_required)):
    o = db.get(SubOrder, order_id)
    if o:
        o.order_no = order_no
        o.total_amount = total_amount
        o.order_date = _date(order_date)
        o.note = note
        db.commit()
        return RedirectResponse(url=f"/subcontracts/{o.subcontract_id}", status_code=303)
    return RedirectResponse(url="/subcontracts", status_code=303)


@router.post("/orders/{order_id}/delete")
def suborder_delete(order_id: int, request: Request,
                    db: Session = Depends(get_db),
                    user: User = Depends(login_required)):
    o = db.get(SubOrder, order_id)
    if o:
        sid = o.subcontract_id
        db.delete(o)
        db.commit()
        return RedirectResponse(url="/subcontracts", status_code=303)
    return RedirectResponse(url="/subcontracts", status_code=303)


@router.post("/orders/{order_id}/link-invoices")
def suborder_link_invoices(order_id: int, request: Request,
                           invoice_ids: list[int] = Form([]),
                           db: Session = Depends(get_db),
                           user: User = Depends(login_required)):
    """分包采购单：关联发票（同时写入分包合同关联）。"""
    o = db.get(SubOrder, order_id)
    if o is None:
        return RedirectResponse(url="/subcontracts", status_code=303)
    for iid in invoice_ids:
        inv = db.get(Invoice, iid)
        if inv:
            inv.suborder_id = o.id
            inv.subcontract_id = o.subcontract_id
    db.commit()
    return RedirectResponse(url=f"/subcontracts/{o.subcontract_id}", status_code=303)


@router.post("/orders/{order_id}/link-payments")
def suborder_link_payments(order_id: int, request: Request,
                           payment_ids: list[int] = Form([]),
                           db: Session = Depends(get_db),
                           user: User = Depends(login_required)):
    """分包采购单：关联付款（同时写入分包合同关联）。"""
    o = db.get(SubOrder, order_id)
    if o is None:
        return RedirectResponse(url="/subcontracts", status_code=303)
    for pidx in payment_ids:
        pay = db.get(SupplierPayment, pidx)
        if pay:
            pay.suborder_id = o.id
            pay.subcontract_id = o.subcontract_id
    db.commit()
    return RedirectResponse(url=f"/subcontracts/{o.subcontract_id}", status_code=303)


@router.post("/orders/reorder")
async def suborders_reorder(request: Request,
                            db: Session = Depends(get_db),
                            user: User = Depends(login_required)):
    """分包合同详情：拖拽排序后保存分包采购单新顺序。"""
    import json
    try:
        body = await request.body()
        data = json.loads(body or b"{}")
    except Exception:
        data = {}
    ids = data.get("ids", [])
    subs = {int(o.id): o for o in db.query(SubOrder).all()}
    for idx, oid in enumerate(ids):
        o = subs.get(int(oid))
        if o:
            o.sort_order = idx + 1
    db.commit()
    return {"ok": True}


@router.get("/{subcontract_id}")
def subcontract_detail(subcontract_id: int, request: Request,
                       db: Session = Depends(get_db),
                       user: User = Depends(login_required)):
    sc = db.get(SubContract, subcontract_id)
    if sc is None:
        return RedirectResponse(url="/subcontracts", status_code=303)
    ordered = sum(o.total_amount for o in sc.orders)
    remain = round(sc.amount - ordered, 2) if sc.amount else 0
    progress = round(ordered / sc.amount * 100, 1) if sc.amount else 0
    invoices = db.query(Invoice).filter(
        Invoice.subcontract_id == subcontract_id).order_by(Invoice.invoice_date.asc(), Invoice.id.asc()).all()
    payments = db.query(SupplierPayment).filter(
        SupplierPayment.subcontract_id == subcontract_id).order_by(SupplierPayment.pay_date.asc()).all()
    # 关联分包采购单号 + 各分包单已开票/已付款
    so_map = {so.id: so for so in db.query(SubOrder).filter(SubOrder.subcontract_id == subcontract_id).all()}
    for i in invoices:
        i.so_no = (so_map[i.suborder_id].order_no or f"分包单#{i.suborder_id}") if i.suborder_id in so_map else ""
    for p in payments:
        p.so_no = (so_map[p.suborder_id].order_no or f"分包单#{p.suborder_id}") if p.suborder_id in so_map else ""
    for so in sc.orders:
        so.invoice_amt = sum(x.amount for x in invoices if x.suborder_id == so.id and x.status != "作废")
        so.paid_amt = sum(x.amount for x in payments
                          if x.suborder_id == so.id and x.status != "作废")
    invoice_total = sum(i.amount for i in invoices if i.status != "作废")
    paid_total = sum(p.amount for p in payments)
    remain_invoice = max(sc.amount - invoice_total, 0) if sc.amount else 0
    remain_pay = max(sc.amount - paid_total, 0) if sc.amount else 0
    over_pay = (paid_total or 0) - (sc.amount or 0)
    projects = db.query(Project).order_by(Project.name).all()
    response = render(request, "subcontracts/detail.html", {
        "active": "subcontracts", "user": user, "sc": sc,
        # 审批留痕：分包合同详情页展示审批状态
        "approval_entity": "sub_contract",
        "approval_id": subcontract_id,
        "ordered": ordered, "ordered_pct": progress,
        "remain": remain,
        "remain_pct": round(max(remain, 0) / sc.amount * 100, 1) if sc.amount else 0,
        "progress": progress,
        "invoices": invoices, "payments": payments,
        "invoice_total": invoice_total,
        "inv_pct": round(invoice_total / sc.amount * 100, 1) if sc.amount else 0,
        "paid_total": paid_total,
        "paid_pct": round(paid_total / sc.amount * 100, 1) if sc.amount else 0,
        "remain_pay": remain_pay,
        "remain_pay_pct": round(remain_pay / sc.amount * 100, 1) if sc.amount else 0,
        "over_pay": over_pay,
        "remain_invoice": remain_invoice,
        "remain_inv_pct": round(remain_invoice / sc.amount * 100, 1) if sc.amount else 0,
        "projects": projects,
        "unlinked_invoices": db.query(Invoice).filter(
            Invoice.subcontract_id == subcontract_id,
            Invoice.suborder_id.is_(None),
            Invoice.direction == "进项",
            Invoice.status != "作废",
        ).order_by(Invoice.invoice_date.desc(), Invoice.id.desc()).all(),
        "unlinked_payments": db.query(SupplierPayment).filter(
            SupplierPayment.subcontract_id == subcontract_id,
            SupplierPayment.suborder_id.is_(None),
            not_voided(SupplierPayment),   # 作废的不该再被挂到分包单上
        ).order_by(SupplierPayment.pay_date.desc(), SupplierPayment.id.desc()).all(),
        "categories": CATEGORIES,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/{subcontract_id}/materials")
def submaterial_create(subcontract_id: int, request: Request,
                       name: str = Form(""), spec: str = Form(""), unit: str = Form(""),
                       quantity: float = Form(0), unit_price: float = Form(0),
                       db: Session = Depends(get_db),
                       user: User = Depends(login_required)):
    sc = db.get(SubContract, subcontract_id)
    if sc:
        amt = round(quantity * unit_price, 2)
        db.add(SubContractMaterial(subcontract_id=subcontract_id, name=name,
                                   spec=spec, unit=unit, quantity=quantity,
                                   unit_price=unit_price, amount=amt))
        db.commit()
    return RedirectResponse(url=f"/subcontracts/{subcontract_id}#materials", status_code=303)


@router.post("/materials/{mid}/delete")
def submaterial_delete(mid: int, request: Request,
                       db: Session = Depends(get_db),
                       user: User = Depends(login_required)):
    m = db.get(SubContractMaterial, mid)
    if m:
        sid = m.subcontract_id
        db.delete(m)
        db.commit()
        return RedirectResponse(url=f"/subcontracts/{sid}#materials", status_code=303)
    return RedirectResponse(url="/subcontracts", status_code=303)


@router.post("/{subcontract_id}/milestones")
def submilestone_create(subcontract_id: int, request: Request,
                        name: str = Form(""), m_date: str = Form(""), amount: float = Form(0),
                        db: Session = Depends(get_db),
                        user: User = Depends(login_required)):
    sc = db.get(SubContract, subcontract_id)
    if sc:
        db.add(SubContractMilestone(subcontract_id=subcontract_id, name=name,
                                    m_date=_date(m_date), amount=amount))
        db.commit()
    return RedirectResponse(url=f"/subcontracts/{subcontract_id}#milestones", status_code=303)


@router.post("/milestones/{ms_id}/delete")
def submilestone_delete(ms_id: int, request: Request,
                        db: Session = Depends(get_db),
                        user: User = Depends(login_required)):
    m = db.get(SubContractMilestone, ms_id)
    if m:
        sid = m.subcontract_id
        db.delete(m)
        db.commit()
        return RedirectResponse(url=f"/subcontracts/{sid}#milestones", status_code=303)
    return RedirectResponse(url="/subcontracts", status_code=303)
