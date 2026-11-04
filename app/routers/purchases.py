"""采购管理：采购单、明细、到货入库。"""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..utils.files import save_upload
from ..models import (
    ContractMaterial, InventoryRecord, Invoice, Material, PO_STATUS, Project, ProjectMaterial,
    PurchaseContract, PurchaseOrder, PurchaseOrderItem, Supplier, SupplierPayment, User,
)
from ..utils.audit import log_audit
from ..voided import not_voided
from ..web import render

router = APIRouter(prefix="/purchases", tags=["purchases"])


def _date(v: str):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _f(v):
    try:
        return float(v) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def find_or_create_material(db: Session, name: str, spec: str, unit: str) -> Material:
    """按名称（优先名称+规格+单位）匹配材料库，匹配不到自动创建。
    单位不一致时不复用，避免采购单显示错误单位。"""
    candidates = db.query(Material).filter(func.lower(Material.name) == name.lower()).all()
    if candidates:
        _spec = (spec or "").strip()
        _unit = (unit or "").strip()
        if _spec:
            for m in candidates:
                if (m.spec or "").strip() == _spec and (m.unit or "").strip() == _unit:
                    return m
            for m in candidates:
                if (m.spec or "").strip() == _spec:
                    return m
        else:
            for m in candidates:
                if (m.unit or "").strip() == _unit:
                    return m
        return candidates[0]
    material = Material(name=name, spec=spec or "", unit=unit or "件", ref_price=0)
    db.add(material)
    db.flush()
    return material


@router.get("")
def purchase_list(request: Request, status: str = "",
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    query = db.query(PurchaseOrder)
    if status:
        query = query.filter(PurchaseOrder.status == status)
    orders = query.order_by(PurchaseOrder.created_at.desc()).all()
    return render(request, "purchases/list.html", {
        "active": "purchases", "user": user, "orders": orders, "status": status,
    })


@router.get("/new")
def purchase_new(request: Request, contract_id: int = 0,
                 back_url: str = "",
                 db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    projects = db.query(Project).order_by(Project.name).all()
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    contracts = db.query(PurchaseContract).order_by(PurchaseContract.sign_date.desc()).all()
    prefill_supplier_id = 0
    prefill_project_id = 0
    if contract_id:
        _pc = db.get(PurchaseContract, contract_id)
        if _pc:
            prefill_supplier_id = _pc.supplier_id or 0
            prefill_project_id = _pc.project_id or 0
    response = render(request, "purchases/form.html", {
        "active": "purchases", "user": user, "projects": projects,
        "suppliers": suppliers, "contracts": contracts, "order": None,
        "prefill_contract_id": contract_id,
        "prefill_supplier_id": prefill_supplier_id,
        "prefill_project_id": prefill_project_id,
        "back_url": back_url or (f"/contracts/{contract_id}" if contract_id else ""),
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/new")
def purchase_create(request: Request,
                    project_id: int = Form(0), supplier_name: str = Form(""),
                    supplier_id: int = Form(0),
                    contract_id: int = Form(0),
                    order_no: str = Form(""), order_date: str = Form(""), note: str = Form(""),
                    back_url: str = Form(""),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not supplier_id and supplier_name:
        _sn = supplier_name.strip()
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                supplier_id = _s.id
                break
    # 选了合同但未选供应商/项目时，自动采用合同的供应商/项目
    if not supplier_id and contract_id:
        contract = db.get(PurchaseContract, contract_id)
        if contract and contract.supplier_id:
            supplier_id = contract.supplier_id
    if not project_id and contract_id:
        contract = db.get(PurchaseContract, contract_id)
        if contract and contract.project_id:
            project_id = contract.project_id
    order = PurchaseOrder(project_id=project_id or None, supplier_id=supplier_id or None,
                          contract_id=contract_id or None,
                          order_no=order_no, order_date=_date(order_date),
                          status="待下单", note=note)
    db.add(order)
    db.commit()
    log_audit(db, user, "新增", "采购单", order.id, f"{order.order_no or ('采购单#' + str(order.id))}")
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url=f"/purchases/{order.id}", status_code=303)


@router.get("/{order_id}")
def purchase_detail(order_id: int, request: Request, back_url: str = "",
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        return RedirectResponse(url="/purchases", status_code=303)
    materials = db.query(Material).order_by(Material.name).all()
    # 所属合同执行进度与时间节点
    contract = order.contract
    contract_ordered = contract_progress = order_percent = 0
    if contract:
        contract_orders = db.query(PurchaseOrder).filter(
            PurchaseOrder.contract_id == contract.id).all()
        contract_ordered = sum(po.total_amount for po in contract_orders)
        contract_progress = round(contract_ordered / contract.amount * 100, 1) if contract.amount else 0
        order_percent = round(order.total_amount / contract.amount * 100, 1) if contract.amount else 0
    order_invoices = db.query(Invoice).filter(
        Invoice.po_id == order_id).order_by(Invoice.invoice_date.asc(), Invoice.id.asc()).all()
    unlinked_invoices = db.query(Invoice).filter(
        Invoice.po_id.is_(None), Invoice.direction == "进项", Invoice.status != "作废",
        Invoice.contract_id == order.contract_id
    ).order_by(Invoice.invoice_date.desc(), Invoice.id.desc()).all()
    order_payments = db.query(SupplierPayment).filter(
        SupplierPayment.po_id == order_id).order_by(SupplierPayment.pay_date.asc()).all()
    unlinked_payments = db.query(SupplierPayment).filter(
        SupplierPayment.po_id.is_(None),
        SupplierPayment.contract_id == order.contract_id,
        not_voided(SupplierPayment),   # 作废的不再作为可关联候选
    ).order_by(SupplierPayment.pay_date.desc(), SupplierPayment.id.desc()).all()
    order_invoice_total = sum(i.amount for i in order_invoices if i.status != "作废")
    order_paid_total = sum(p.amount for p in order_payments if p.status != "作废")
    contract_materials = []
    contract_qty = {}
    contract_price = {}
    if order.contract_id:
        contract_materials = db.query(Material).filter(
            Material.contract_id == order.contract_id).order_by(Material.name).all()
        for cm in db.query(ContractMaterial).filter(
                ContractMaterial.contract_id == order.contract_id).all():
            _key = (cm.name or "") + "||" + (cm.spec or "")
            contract_qty[_key] = cm.quantity or 0
            contract_price[_key] = cm.unit_price or 0
    other_materials = [m for m in materials if m.id not in {x.id for x in contract_materials}]
    return render(request, "purchases/detail.html", {
        "back_url": back_url, "active": "purchases", "user": user, "order": order,
        "materials": materials, "contract_materials": contract_materials,
        "other_materials": other_materials, "contract_qty": contract_qty,
        "contract_price": contract_price,
        "contract_ordered": contract_ordered, "contract_progress": contract_progress,
        "order_percent": order_percent,
        "order_invoices": order_invoices, "order_payments": order_payments,
        "order_invoice_total": order_invoice_total, "order_paid_total": order_paid_total,
        "unlinked_invoices": unlinked_invoices,
        "unlinked_payments": unlinked_payments,
    })


@router.get("/{order_id}/edit")
def purchase_edit(order_id: int, request: Request, back_url: str = "",
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        return RedirectResponse(url="/purchases", status_code=303)
    return render(request, "purchases/form.html", {
        "active": "purchases", "user": user, "back_url": back_url, "order": order,
        "projects": db.query(Project).order_by(Project.name).all(),
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "contracts": db.query(PurchaseContract).order_by(PurchaseContract.sign_date.desc()).all(),
    })


@router.post("/{order_id}/edit")
def purchase_update(order_id: int, request: Request,
                    project_id: int = Form(0), supplier_name: str = Form(""),
                    supplier_id: int = Form(0),
                    contract_id: int = Form(0),
                    order_no: str = Form(""), order_date: str = Form(""), note: str = Form(""),
                    back_url: str = Form(""),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        return RedirectResponse(url="/purchases", status_code=303)
    if not supplier_id and contract_id:
        contract = db.get(PurchaseContract, contract_id)
        if contract and contract.supplier_id:
            supplier_id = contract.supplier_id
    if not project_id and contract_id:
        contract = db.get(PurchaseContract, contract_id)
        if contract and contract.project_id:
            project_id = contract.project_id
    order.project_id = project_id or None
    if not supplier_id and supplier_name:
        _sn = supplier_name.strip()
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                supplier_id = _s.id
                break
    order.supplier_id = supplier_id or None
    order.contract_id = contract_id or None
    order.order_no = order_no
    order.order_date = _date(order_date)
    order.note = note
    db.commit()
    log_audit(db, user, "修改", "采购单", order_id, f"{order.order_no or ''}")
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)


@router.post("/{order_id}/items")
def purchase_item_create(order_id: int, request: Request,
                         material_id: int = Form(0), quantity: float = Form(0),
                         unit_price: float = Form(0), material_name: str = Form(""),
                         db: Session = Depends(get_db), user: User = Depends(login_required)):
    order = db.get(PurchaseOrder, order_id)
    if order:
        # 材料库为空时手动输入材料名 → 自动创建材料
        if not material_id and material_name.strip():
            m = db.query(Material).filter(Material.name == material_name.strip()).first()
            if not m:
                m = Material(name=material_name.strip(), spec="", unit="")
                db.add(m)
                db.flush()
            material_id = m.id
        existing = db.query(PurchaseOrderItem).filter_by(po_id=order_id, material_id=material_id).first()
        if existing:
            existing.quantity += quantity
            if unit_price:
                existing.unit_price = unit_price
            if m and m.unit:
                existing.unit = m.unit
        else:
            db.add(PurchaseOrderItem(po_id=order_id, material_id=material_id,
                                     quantity=quantity, unit_price=unit_price,
                                     unit=(m.unit if m else "") or ""))
        db.commit()
    return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)


@router.post("/{order_id}/items-from-contract")
def purchase_items_from_contract(order_id: int, request: Request,
                                 cm_ids: list[int] = Form([]),
                                 cm_qty: list[str] = Form([]),
                                 db: Session = Depends(get_db),
                                 user: User = Depends(login_required)):
    """从采购合同清单勾选材料，按每项输入的数量批量添加到采购单明细（支持分批采购）。"""
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        return RedirectResponse(url="/purchases", status_code=303)
    added = merged = 0
    contract_materials = db.query(ContractMaterial).filter(
        ContractMaterial.id.in_(cm_ids)).all()
    # 采购单未设供应商/项目时，从选中材料带出
    if not order.supplier_id:
        s_id = next((cm.supplier_id for cm in contract_materials if cm.supplier_id), None)
        if s_id:
            order.supplier_id = s_id
    if not order.project_id:
        p_id = next((cm.project_id for cm in contract_materials if cm.project_id), None)
        if p_id:
            order.project_id = p_id
    for i, cm in enumerate(contract_materials):
        if order.contract_id and cm.contract_id != order.contract_id:
            continue
        qty = _f(cm_qty[i]) if i < len(cm_qty) else cm.quantity
        if qty <= 0:
            continue
        material = find_or_create_material(db, cm.name, cm.spec, cm.unit)
        existing = db.query(PurchaseOrderItem).filter_by(
            po_id=order.id, material_id=material.id).first()
        if existing:
            existing.quantity += qty
            if cm.unit_price:
                existing.unit_price = cm.unit_price
            if cm.unit:
                existing.unit = cm.unit
            merged += 1
        else:
            db.add(PurchaseOrderItem(po_id=order.id, material_id=material.id,
                                     quantity=qty, unit_price=cm.unit_price, unit=cm.unit or ""))
            added += 1
    db.commit()
    return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)


@router.post("/{order_id}/link-invoices")
def purchase_link_invoices(order_id: int, request: Request,
                           invoice_ids: list[int] = Form([]),
                           db: Session = Depends(get_db),
                           user: User = Depends(login_required)):
    for iid in invoice_ids:
        inv = db.get(Invoice, iid)
        if inv:
            inv.po_id = order_id
    db.commit()
    return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)


@router.post("/{order_id}/unlink-invoice/{invoice_id}")
def purchase_unlink_invoice(order_id: int, invoice_id: int, request: Request,
                            db: Session = Depends(get_db),
                            user: User = Depends(login_required)):
    inv = db.get(Invoice, invoice_id)
    if inv and inv.po_id == order_id:
        inv.po_id = None
        db.commit()
    return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)


@router.post("/{order_id}/status")
def purchase_status_update(order_id: int, status: str = Form(""),
                           db: Session = Depends(get_db),
                           user: User = Depends(login_required)):
    """AJAX：直接修改采购单状态（采购管理 / 合同详情页行内下拉）。"""
    o = db.get(PurchaseOrder, order_id)
    if o is None:
        return {"ok": False, "msg": "采购单不存在"}
    old = o.status
    if status in PO_STATUS:
        o.status = status
        db.commit()
        if old != status:
            log_audit(db, user, "修改", "采购单", o.id, f"状态 {old} → {status}")
    return {"ok": True, "status": o.status}


@router.post("/{order_id}/link-payments")
def purchase_link_payments(order_id: int, request: Request,
                           payment_ids: list[int] = Form([]),
                           db: Session = Depends(get_db),
                           user: User = Depends(login_required)):
    for pid in payment_ids:
        p = db.get(SupplierPayment, pid)
        if p:
            p.po_id = order_id
    db.commit()
    return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)


@router.post("/{order_id}/unlink-payment/{payment_id}")
def purchase_unlink_payment(order_id: int, payment_id: int, request: Request,
                            db: Session = Depends(get_db),
                            user: User = Depends(login_required)):
    p = db.get(SupplierPayment, payment_id)
    if p and p.po_id == order_id:
        p.po_id = None
        db.commit()
    return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)


@router.post("/items/{item_id}/update")
def purchase_item_update(item_id: int, request: Request,
                         quantity: float = Form(0), unit_price: float = Form(0),
                         back_url: str = Form(""),
                         db: Session = Depends(get_db),
                         user: User = Depends(login_required)):
    it = db.get(PurchaseOrderItem, item_id)
    if it:
        it.quantity = quantity
        if unit_price:
            it.unit_price = unit_price
        db.commit()
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url=f"/purchases/{it.po_id}", status_code=303) if it else RedirectResponse(url="/purchases", status_code=303)


@router.post("/items/{item_id}/delete")
def purchase_item_delete(item_id: int, request: Request, back_url: str = Form(""),
                         db: Session = Depends(get_db), user: User = Depends(login_required)):
    item = db.get(PurchaseOrderItem, item_id)
    oid = item.po_id if item else 0
    if item:
        db.delete(item)
        db.commit()
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url=f"/purchases/{oid}", status_code=303)



@router.post("/{order_id}/import-excel")
def purchase_import_excel(order_id: int, request: Request, file: UploadFile = File(...),
                          db: Session = Depends(get_db),
                          user: User = Depends(login_required)):
    """Excel 导入材料明细：列=材料/材料名称、规格、数量、单价。按名称匹配材料，不存在则自动创建。"""
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        return RedirectResponse(url="/purchases", status_code=303)
    fname = (file.filename or "").lower()
    if not (fname.endswith(".xlsx") or fname.endswith(".xls") or fname.endswith(".csv")):
        return RedirectResponse(url=f"/purchases/{order_id}?import_err=文件格式需为 Excel/CSV", status_code=303)
    data = file.file.read()
    import io
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return RedirectResponse(url=f"/purchases/{order_id}?import_err=Excel 为空", status_code=303)
    header = [str(c or "").strip().lower() for c in rows[0]]
    def col(*names):
        for i, h in enumerate(header):
            if any(n in h for n in names):
                return i
        return -1
    i_name = col("材料", "材料名称", "名称", "material", "品名", "物料")
    i_spec = col("规格", "spec", "型号")
    i_qty = col("数量", "qty", "quantity")
    i_price = col("单价", "价格", "price", "unit")
    if i_name < 0 or i_qty < 0 or i_price < 0:
        return RedirectResponse(url=f"/purchases/{order_id}?import_err=缺少必需列（材料/数量/单价）", status_code=303)
    added = 0
    for r in rows[1:]:
        name = str(r[i_name] or "").strip()
        if not name:
            continue
        try:
            qty = float(r[i_qty] or 0)
        except (TypeError, ValueError):
            qty = 0
        try:
            price = float(r[i_price] or 0)
        except (TypeError, ValueError):
            price = 0
        spec = str(r[i_spec]).strip() if i_spec >= 0 and r[i_spec] else ""
        mat = None
        for _m in db.query(Material).all():
            if _m.name == name:
                mat = _m
                break
        if mat is None:
            mat = Material(name=name, spec=spec or "", unit="", ref_price=price,
                           low_stock=0)
            db.add(mat)
            db.flush()
        db.add(PurchaseOrderItem(po_id=order.id, material_id=mat.id,
                                 quantity=qty, unit_price=price, unit=mat.unit or ""))
        added += 1
    db.commit()
    if added == 0:
        return RedirectResponse(url=f"/purchases/{order_id}?import_err=未识别到有效数据行", status_code=303)
    return RedirectResponse(url=f"/purchases/{order_id}?import_ok={added}", status_code=303)



@router.post("/{order_id}/items-batch")
def purchase_items_batch(order_id: int, request: Request,
                         material_ids: list[int] = Form([]),
                         quantities: list[float] = Form([]),
                         unit_prices: list[float] = Form([]),
                         back_url: str = Form(""),
                         db: Session = Depends(get_db), user: User = Depends(login_required)):
    """批量添加材料明细（多选+各自用量/单价）：同材料合并累加数量。"""
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        return RedirectResponse(url="/purchases", status_code=303)
    n = 0
    for idx, (mid, qty) in enumerate(zip(material_ids, quantities)):
        if not mid or qty is None or qty <= 0:
            continue
        material = db.get(Material, mid)
        if material is None:
            continue
        price = 0.0
        if idx < len(unit_prices) and unit_prices[idx]:
            price = unit_prices[idx]
        if not price:
            price = material.ref_price or 0
        it = db.query(PurchaseOrderItem).filter_by(po_id=order.id, material_id=mid).first()
        if it:
            it.quantity = round((it.quantity or 0) + qty, 2)
            if not it.unit_price:
                it.unit_price = price
            if material.unit:
                it.unit = material.unit
        else:
            db.add(PurchaseOrderItem(po_id=order.id, material_id=mid,
                                     quantity=qty, unit_price=price, unit=material.unit or ""))
        n += 1
    db.commit()
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)



@router.post("/{order_id}/items-batch-delete")
def purchase_items_batch_delete(order_id: int, request: Request,
                                item_ids: list[int] = Form([]),
                                back_url: str = Form(""),
                                db: Session = Depends(get_db), user: User = Depends(login_required)):
    """批量删除材料明细。"""
    n = 0
    for iid in item_ids:
        it = db.get(PurchaseOrderItem, iid)
        if it and it.po_id == order_id:
            db.delete(it)
            n += 1
    db.commit()
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)


@router.post("/{order_id}/status")
def purchase_status(order_id: int, request: Request, status: str = Form("待下单"),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    order = db.get(PurchaseOrder, order_id)
    if order:
        order.status = status if status in ("待下单", "已下单", "部分到货", "已到货") else "待下单"
        db.commit()
    return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)


@router.post("/{order_id}/receive")
def purchase_receive(order_id: int, request: Request,
                     db: Session = Depends(get_db), user: User = Depends(login_required)):
    """标记到货：生成库存入库流水，并累计到项目材料已采购量（幂等保护）。"""
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        return RedirectResponse(url="/purchases", status_code=303)
    if order.status == "已到货":
        # 已到货不允许重复入库，避免重复累计
        return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)
    order.status = "已到货"
    for item in order.items:
        db.add(InventoryRecord(
            material_id=item.material_id, record_type="入库", quantity=item.quantity,
            project_id=order.project_id, related_po_id=order_id, note="采购到货"))
        # 累计项目材料已采购量
        if order.project_id:
            pm = db.query(ProjectMaterial).filter_by(
                project_id=order.project_id, material_id=item.material_id).first()
            if pm:
                pm.purchased_quantity += item.quantity
    db.commit()
    return RedirectResponse(url=f"/purchases/{order_id}", status_code=303)


@router.post("/{order_id}/delete")
def purchase_delete(order_id: int, request: Request, back_url: str = Form(""),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    order = db.get(PurchaseOrder, order_id)
    if order:
        _ono = order.order_no or ""
        db.delete(order)
        db.commit()
        log_audit(db, user, "删除", "采购单", order_id, _ono)
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url="/purchases", status_code=303)
