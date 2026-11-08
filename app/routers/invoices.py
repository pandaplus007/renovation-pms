"""发票管理：进项/销项发票登记、PDF 上传识别、筛选、汇总、导出。"""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import login_required
from ..config import DATA_DIR
from ..database import get_db
from ..models import (Contract, Customer, Invoice, Payment, Project, PurchaseContract,
                      PurchaseOrder, SubContract, SubOrder, Supplier, User)
from ..utils.excel_export import export_xlsx
from ..utils.files import save_upload
from ..utils.audit import log_audit
from ..utils.invoice_parser import (
    extract_pdf_text, match_customer_project, match_supplier, parse_invoice_text,
)
from ..web import render

router = APIRouter(prefix="/invoices", tags=["invoices"])


def _date(v: str):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _sync_payment_from_invoice(db: Session, invoice: Invoice):
    """销项发票关联项目时，自动同步项目收款记录（标记已开发票）。
    已关联收款节点（payment_id）的发票不自动生成，避免重复。"""
    if invoice.direction != "销项" or not invoice.project_id:
        return
    if invoice.payment_id:
        return
    existing = db.query(Payment).filter(
        Payment.project_id == invoice.project_id,
        Payment.note.like(f"invoice:{invoice.id}%")).first()
    if invoice.status == "作废":
        if existing:
            db.delete(existing)
        return
    name = f"已开发票 {invoice.invoice_no or ''}".strip()
    if existing:
        existing.name = name
        existing.plan_amount = invoice.amount
        existing.plan_date = invoice.invoice_date
        existing.note = f"invoice:{invoice.id} 发票自动生成"
    else:
        db.add(Payment(project_id=invoice.project_id, name=name,
                       plan_amount=invoice.amount, plan_date=invoice.invoice_date,
                       paid_amount=0, note=f"invoice:{invoice.id} 发票自动生成"))


def _remove_payment_from_invoice(db: Session, invoice_id: int):
    """删除发票时同步删除对应收款记录。"""
    for p in db.query(Payment).filter(Payment.note.like(f"invoice:{invoice_id}%")).all():
        db.delete(p)


def _form_ctx(db: Session, user: User, invoice=None, parsed=None, file_path="",
              match=None, error="", contract_id: int = 0):
    """发票表单页公共上下文。
    contract_id 非 0 时，关联采购单下拉只列出该合同的采购单，避免混入其他合同采购单。
    """
    po_query = db.query(PurchaseOrder)
    if contract_id:
        po_query = po_query.filter(PurchaseOrder.contract_id == contract_id)
    return {
        "active": "invoices", "user": user, "invoice": invoice,
        "parsed": parsed, "file_path": file_path, "match": match, "error": error,
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "customers": db.query(Customer).order_by(Customer.name).all(),
        "projects": db.query(Project).order_by(Project.name).all(),
        "pos": po_query.order_by(PurchaseOrder.id.desc()).all(),
        "contracts": db.query(PurchaseContract).order_by(PurchaseContract.sign_date.desc()).all(),
    }


@router.get("")
def invoice_list(request: Request, direction: list[str] = Query([]),
                 invoice_type: list[str] = Query([]), status: list[str] = Query([]),
                 supplier_id: list[int] = Query([]), project_id: list[int] = Query([]),
                 sort: str = "date_desc",
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    query = db.query(Invoice)
    if direction:
        query = query.filter(Invoice.direction.in_(direction))
    if invoice_type:
        query = query.filter(Invoice.invoice_type.in_(invoice_type))
    if status:
        query = query.filter(Invoice.status.in_(status))
    if supplier_id:
        query = query.filter(Invoice.supplier_id.in_(supplier_id))
    if project_id:
        query = query.filter(Invoice.project_id.in_(project_id))
    if sort == "date_asc":
        invoices = query.order_by(Invoice.invoice_date.is_(None), Invoice.invoice_date.asc(), Invoice.id.asc()).all()
    elif sort == "amount_desc":
        invoices = query.order_by(Invoice.amount.desc(), Invoice.id.desc()).all()
    elif sort == "amount_asc":
        invoices = query.order_by(Invoice.amount.asc(), Invoice.id.asc()).all()
    elif sort == "no_desc":
        invoices = query.order_by(Invoice.invoice_no.desc(), Invoice.id.desc()).all()
    elif sort == "no_asc":
        invoices = query.order_by(Invoice.invoice_no.asc(), Invoice.id.asc()).all()
    else:
        invoices = query.order_by(Invoice.invoice_date.is_(None), Invoice.invoice_date.desc(), Invoice.id.desc()).all()
    # 关联采购单/分包单号（供列表显示与筛选）
    po_map = {p.id: p for p in db.query(PurchaseOrder).all()}
    so_map = {s.id: s for s in db.query(SubOrder).all()}
    pc_map = {p.id: p for p in db.query(PurchaseContract).all()}
    sc_map = {s.id: s for s in db.query(SubContract).all()}
    for inv in invoices:
        inv.po_no = (po_map[inv.po_id].order_no or f"采购单#{inv.po_id}") if inv.po_id in po_map else ""
        inv.suborder_no = (so_map[inv.suborder_id].order_no or f"分包单#{inv.suborder_id}") if inv.suborder_id in so_map else ""
        inv.contract_name = (pc_map[inv.contract_id].name if inv.contract_id in pc_map else "") or (sc_map[inv.subcontract_id].name if inv.subcontract_id in sc_map else "")

    total_in = sum(i.amount for i in invoices if i.direction == "进项" and i.status != "作废")
    total_out = sum(i.amount for i in invoices if i.direction == "销项" and i.status != "作废")

    return render(request, "invoices/list.html", {
        "active": "invoices", "user": user, "invoices": invoices,
        "direction": direction, "invoice_type": invoice_type, "status": status,
        "supplier_id": supplier_id, "project_id": project_id, "sort": sort,
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "projects": db.query(Project).order_by(Project.name).all(),
        "total_in": total_in, "total_out": total_out,
    })


@router.get("/export")
def invoice_export(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(login_required)):
    invoices = db.query(Invoice).order_by(Invoice.invoice_date.desc()).all()
    data = []
    for i in invoices:
        data.append([
            i.direction, i.invoice_type, i.invoice_no, i.amount, i.tax_amount,
            str(i.invoice_date or ""), i.title, i.tax_no,
            i.seller_name, i.seller_tax_no,
            i.supplier.name if i.supplier else "",
            i.customer.name if i.customer else "",
            i.project.name if i.project else "",
            i.status, i.note,
        ])
    return export_xlsx(
        "发票清单.xlsx",
        ["方向", "发票类型", "发票号", "金额", "税额", "开票日期",
         "购方名称", "购方税号", "销方名称", "销方税号",
         "供应商", "客户", "项目", "状态", "备注"],
        data, "发票清单",
    )


@router.get("/new")
def invoice_new(request: Request, project_id: int = 0, payment_id: int = 0,
                contract_id: int = 0, po_id: int = 0, subcontract_id: int = 0,
                suborder_id: int = 0, amount: float = 0, title: str = "",
                back_url: str = "", next: str = "",
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    # 从采购合同详情页登记发票：优先过滤该合同的采购单
    ctx = _form_ctx(db, user, contract_id=contract_id)
    ctx["prefill_po_id"] = po_id
    ctx["prefill_subcontract_id"] = subcontract_id
    ctx["prefill_suborder_id"] = suborder_id
    # 分包合同详情登记发票：自动带出项目 + 销方（乙方）供应商
    if subcontract_id:
        _sc = db.get(SubContract, subcontract_id)
        if _sc:
            if not project_id and _sc.project_id:
                project_id = _sc.project_id
            if _sc.party_b:
                ctx["prefill_seller_name"] = _sc.party_b
                for _s in db.query(Supplier).all():
                    if _s.name == _sc.party_b or (_sc.party_b in (_s.name or "") or (_s.name or "") in _sc.party_b):
                        ctx["prefill_supplier_id"] = _s.id
                        break
    # 从采购合同详情页登记发票（进项）：自动带出项目 + 销方（合同供应商）
    if contract_id:
        _pc = db.get(PurchaseContract, contract_id)
        if _pc:
            if not project_id and _pc.project_id:
                project_id = _pc.project_id
            if _pc.supplier:
                ctx["prefill_seller_name"] = _pc.supplier.name
                ctx["prefill_supplier_id"] = _pc.supplier.id
    ctx["prefill_project_id"] = project_id
    ctx["suborders"] = (db.query(SubOrder).filter(SubOrder.subcontract_id == subcontract_id).order_by(SubOrder.id.desc()).all()
                        if subcontract_id else [])
    ctx["subcontracts"] = db.query(SubContract).order_by(SubContract.contract_no.desc(), SubContract.id.desc()).all()
    ctx["prefill_payment_id"] = payment_id
    ctx["prefill_contract_id"] = contract_id
    _sel_sup_name = ctx.get("prefill_seller_name", "")
    _sid = ctx.get("prefill_supplier_id", 0)
    if _sid:
        for _s in db.query(Supplier).all():
            if _s.id == _sid:
                _sel_sup_name = _s.name
                break
    ctx["sel_sup_name"] = _sel_sup_name
    ctx["prefill_amount"] = amount
    ctx["prefill_title"] = title
    ctx["back_url"] = back_url or next or ""
    response = render(request, "invoices/form.html", ctx)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/parse")
async def invoice_parse(request: Request, file: UploadFile | None = File(None),
                        project_id: int = Form(0), contract_id: int = Form(0),
                        subcontract_id: int = Form(0), suborder_id: int = Form(0),
                        back_url: str = Form(""),
                        db: Session = Depends(get_db), user: User = Depends(login_required)):
    """上传 PDF 发票 → 提取文本 → 规则解析 → 自动匹配客户/供应商/项目 → 预填表单。"""
    ctx = _form_ctx(db, user, contract_id=contract_id)
    ctx["prefill_project_id"] = project_id
    ctx["prefill_contract_id"] = contract_id
    ctx["prefill_subcontract_id"] = subcontract_id
    ctx["prefill_suborder_id"] = suborder_id
    ctx["suborders"] = (db.query(SubOrder).filter(SubOrder.subcontract_id == subcontract_id).order_by(SubOrder.id.desc()).all()
                        if subcontract_id else [])
    ctx["subcontracts"] = db.query(SubContract).order_by(SubContract.contract_no.desc(), SubContract.id.desc()).all()
    ctx["back_url"] = back_url
    # 从分包/采购合同进入：带出销方（乙方/合同供应商），识别后保留
    if subcontract_id:
        _sc = db.get(SubContract, subcontract_id)
        if _sc and _sc.party_b:
            ctx["prefill_seller_name"] = _sc.party_b
            for _s in db.query(Supplier).all():
                if _s.name == _sc.party_b or (_sc.party_b in (_s.name or "") or (_s.name or "") in _sc.party_b):
                    ctx["prefill_supplier_id"] = _s.id
                    break
    if contract_id:
        _pc = db.get(PurchaseContract, contract_id)
        if _pc and _pc.supplier:
            ctx["prefill_seller_name"] = _pc.supplier.name
            ctx["prefill_supplier_id"] = _pc.supplier.id
    # 没选文件就点「识别并预填」时，浏览器有时连 file 段都不发（而不是发一个空文件）。
    # 若声明成 File(...)，FastAPI 会在进函数之前就回 422 原始 JSON，用户看到的是
    # {"detail":[{"type":"missing","loc":["body","file"],...}]} 这种天书。
    # 所以收 None，统一走模板里的红色提示。
    if file is None or not (file.filename or "").strip():
        ctx["error"] = "请先选择发票 PDF 文件，再点「识别并预填」"
        _resp = render(request, "invoices/form.html", ctx)
        _resp.headers["Cache-Control"] = "no-store"
        return _resp
    if not file.filename.lower().endswith(".pdf"):
        ctx["error"] = "请上传 PDF 格式的发票文件"
        _resp = render(request, "invoices/form.html", ctx)
        _resp.headers["Cache-Control"] = "no-store"
        return _resp

    data = await file.read()
    file.file.seek(0)
    pdf_path = await save_upload(file, "invoice", allowed={".pdf"})
    if not pdf_path:
        ctx["error"] = "PDF 保存失败（可能超过 20MB 限制）"
        return render(request, "invoices/form.html", ctx)
    ctx["file_path"] = pdf_path

    # pdfplumber 对「不是 PDF / 文件损坏」会直接抛 PdfminerException（"No /Root object!"），
    # 不接住就是 500 白页。空文件、别的文件改个后缀、下载中断的 PDF 都会走到这里。
    try:
        text = extract_pdf_text(data)
    except Exception:
        ctx["error"] = ("无法读取这个 PDF（文件可能已损坏、不完整，或其实不是 PDF）。"
                        "请换一份电子发票原文件；PDF 已存档为附件，也可以手动填写。")
        _resp = render(request, "invoices/form.html", ctx)
        _resp.headers["Cache-Control"] = "no-store"
        return _resp
    if not text.strip():
        ctx["error"] = "未能从 PDF 提取文本（该发票可能是扫描件），请手动填写表单。PDF 已保存为附件，可继续登记。"
        return render(request, "invoices/form.html", ctx)

    parsed = parse_invoice_text(text)
    ctx["parsed"] = parsed
    customer, project = match_customer_project(db, parsed.get("title", ""))
    # 购方为"自己公司"时不匹配为客户：公司设置名 + 合同乙方名（采购/施工合同均为本公司）
    if customer:
        import json as _json, os as _os
        _self_names = set()
        try:
            with open(_os.path.join(str(DATA_DIR), "company.json"), encoding="utf-8") as _f:
                _n = (_json.load(_f).get("name") or "").strip()
                if _n:
                    _self_names.add(_n)
        except Exception:
            pass
        for _ct in db.query(Contract).all():
            if _ct.party_b:
                _self_names.add(_ct.party_b.strip())
        for _pc in db.query(PurchaseContract).all():
            if _pc.party_b:
                _self_names.add(_pc.party_b.strip())
        for _sn in _self_names:
            if _sn and (_sn == customer.name or _sn in (customer.name or "") or (customer.name or "") in _sn):
                customer = None
                project = None
                break
    supplier = match_supplier(db, parsed.get("seller", ""))
    # 进项发票：按销方供应商自动匹配采购合同（若该供应商有唯一在售合同则预填）
    contract = None
    if supplier:
        contracts = db.query(PurchaseContract).filter(
            PurchaseContract.supplier_id == supplier.id).all()
        if len(contracts) == 1:
            contract = contracts[0]
    # 用户已带合同（从合同详情进入）时保留原合同；否则用自动匹配的唯一合同
    if not contract_id:
        ctx["prefill_contract_id"] = contract.id if contract else 0
    else:
        ctx["prefill_contract_id"] = contract_id
        # 上传识别时若已指定合同，同步重新过滤采购单下拉
        ctx["pos"] = (db.query(PurchaseOrder).filter(
            PurchaseOrder.contract_id == contract_id)
            .order_by(PurchaseOrder.id.desc()).all())
    ctx["match"] = {
        "customer": customer, "project": project, "supplier": supplier,
        "contract": contract,
        "direction": "进项",
    }
    # 识别后供应商自动等于销方名称（匹配到供应商用供应商名，否则直接销方名）
    _prefill_sup_name = ""
    _pid2 = ctx.get("prefill_supplier_id", 0)
    if _pid2:
        for _s in db.query(Supplier).all():
            if _s.id == _pid2:
                _prefill_sup_name = _s.name
                break
    ctx["sel_sup_name"] = (_prefill_sup_name or ctx.get("prefill_seller_name", "")
                           or (supplier.name if supplier else "")
                           or (parsed.get("seller_name", "") or "")
                           or (parsed.get("seller", "") or ""))
    _resp = render(request, "invoices/form.html", ctx)
    _resp.headers["Cache-Control"] = "no-store"
    return _resp


@router.post("/new")
def invoice_create(request: Request,
                   direction: str = Form("进项"), invoice_type: str = Form("增值税普通发票"),
                   invoice_no: str = Form(""), amount: float = Form(0), tax_amount: float = Form(0), tax_rate: float = Form(0),
                   invoice_date: str = Form(""), title: str = Form(""), tax_no: str = Form(""),
                   seller_name: str = Form(""), seller_tax_no: str = Form(""),
                   back_url: str = Form(""), subcontract_id: int = Form(0),
                   suborder_id: int = Form(0),
                   supplier_name: str = Form(""),
                   supplier_id: int = Form(0), customer_id: int = Form(0),
                   project_id: int = Form(0), po_id: int = Form(0),
                   payment_id: int = Form(0), contract_id: int = Form(0),
                   status: str = Form("正常"), note: str = Form(""),
                   file_path: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not supplier_id and supplier_name:
        _sn = supplier_name.strip()
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                supplier_id = _s.id
                break
    db.add(Invoice(direction=direction, invoice_type=invoice_type, invoice_no=invoice_no,
                   amount=amount, tax_amount=tax_amount, tax_rate=tax_rate, invoice_date=_date(invoice_date),
                   title=title, tax_no=tax_no, seller_name=seller_name, seller_tax_no=seller_tax_no,
                   file_path=file_path,
                   supplier_id=supplier_id or None, customer_id=customer_id or None,
                   project_id=project_id or None, po_id=po_id or None, payment_id=payment_id or None,
                   contract_id=contract_id or None, subcontract_id=subcontract_id or None,
                   suborder_id=suborder_id or None,
                   status=status, note=note))
    db.flush()
    invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
    _sync_payment_from_invoice(db, invoice)
    db.commit()
    log_audit(db, user, "新增", "发票", invoice.id if invoice else None, f"{invoice.invoice_no if invoice else ''} ¥{amount}")
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url="/invoices", status_code=303)


@router.get("/{invoice_id}/edit")
def invoice_edit(invoice_id: int, request: Request, back_url: str = "",
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        return RedirectResponse(url="/invoices", status_code=303)
    ctx = _form_ctx(db, user, invoice=invoice, file_path=invoice.file_path)
    ctx["sel_sup_name"] = invoice.supplier.name if invoice.supplier else ""
    ctx["back_url"] = back_url or ""
    # 编辑分包发票：分包采购单下拉限定在本分包合同
    ctx["prefill_subcontract_id"] = invoice.subcontract_id or 0
    ctx["prefill_suborder_id"] = invoice.suborder_id or 0
    ctx["suborders"] = (db.query(SubOrder).filter(
        SubOrder.subcontract_id == invoice.subcontract_id).order_by(SubOrder.id.desc()).all()
        if invoice.subcontract_id else [])
    ctx["subcontracts"] = db.query(SubContract).order_by(SubContract.contract_no.desc(), SubContract.id.desc()).all()
    return render(request, "invoices/form.html", ctx)


@router.post("/{invoice_id}/edit")
def invoice_update(invoice_id: int, request: Request,
                   direction: str = Form("进项"), invoice_type: str = Form("增值税普通发票"),
                   invoice_no: str = Form(""), amount: float = Form(0), tax_amount: float = Form(0), tax_rate: float = Form(0),
                   invoice_date: str = Form(""), title: str = Form(""), tax_no: str = Form(""),
                   seller_name: str = Form(""), seller_tax_no: str = Form(""),
                   back_url: str = Form(""),
                   supplier_name: str = Form(""),
                   supplier_id: int = Form(0), customer_id: int = Form(0),
                   project_id: int = Form(0), po_id: int = Form(0),
                   payment_id: int = Form(0), contract_id: int = Form(0),
                   status: str = Form("正常"), note: str = Form(""),
                   file_path: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        return RedirectResponse(url="/invoices", status_code=303)
    invoice.direction = direction
    invoice.invoice_type = invoice_type
    invoice.invoice_no = invoice_no
    invoice.amount = amount
    invoice.tax_amount = tax_amount
    invoice.tax_rate = tax_rate
    invoice.invoice_date = _date(invoice_date)
    invoice.title = title
    invoice.tax_no = tax_no
    invoice.seller_name = seller_name
    invoice.seller_tax_no = seller_tax_no
    invoice.file_path = file_path
    if not supplier_id and supplier_name:
        _sn = supplier_name.strip()
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                supplier_id = _s.id
                break
    invoice.supplier_id = supplier_id or None
    invoice.customer_id = customer_id or None
    invoice.project_id = project_id or None
    invoice.po_id = po_id or None
    invoice.payment_id = payment_id or None
    invoice.contract_id = contract_id or None
    invoice.status = status
    invoice.note = note
    _sync_payment_from_invoice(db, invoice)
    db.commit()
    log_audit(db, user, "修改", "发票", invoice.id, f"{invoice.invoice_no or ''} ¥{invoice.amount}")
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url="/invoices", status_code=303)


@router.post("/batch-delete")
def invoice_batch_delete(request: Request, ids: list[int] = Form([]),
                         db: Session = Depends(get_db),
                         user: User = Depends(login_required)):
    for iid in ids:
        inv = db.get(Invoice, iid)
        if inv:
            _remove_payment_from_invoice(db, iid)
            db.delete(inv)
    db.commit()
    log_audit(db, user, "批量删除", "发票", None, f"删除 {len(ids)} 张发票")
    return RedirectResponse(url="/invoices", status_code=303)


@router.post("/{invoice_id}/delete")
def invoice_delete(invoice_id: int, request: Request, back_url: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    invoice = db.get(Invoice, invoice_id)
    if invoice:
        _invno = invoice.invoice_no or ""
        _remove_payment_from_invoice(db, invoice_id)
        db.delete(invoice)
        db.commit()
        log_audit(db, user, "删除", "发票", invoice_id, _invno)
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url="/invoices", status_code=303)
