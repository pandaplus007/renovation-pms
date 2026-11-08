"""采购合同管理：登记、文件识别、存档、时间节点、工期规划。"""
import hashlib
import os
import tempfile
from datetime import date, timedelta

from docx import Document
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import login_required
from ..config import UPLOAD_DIR
from ..database import get_db
from ..models import (CompanyDoc, ContractMaterial, ContractMilestone, DeliveryNote,
                      GanttTask, Invoice, Material, Project, ProjectPhase,
                      PurchaseContract, PurchaseOrder, Staff, Supplier,
                      SupplierPayment, User)
from ..state_colors import STATE_TONE_MAP
from ..utils.contract_parser import (
    KIND_COLORS, milestone_kind, recognize_contract_file,
)
from ..utils.files import DOC_EXTS, IMAGE_EXTS, save_upload
from ..utils.audit import log_audit
from ..voided import not_voided
from ..web import render

router = APIRouter(tags=["contracts"])

def _default_contract_no(db: Session) -> str:
    """生成默认合同编号：C + YYYYMMDD + 两位序号。"""
    from datetime import datetime as _dt
    prefix = "C" + _dt.now().strftime("%Y%m%d")
    n = db.query(PurchaseContract).filter(
        PurchaseContract.contract_no.like(prefix + "%")).count() + 1
    return f"{prefix}{n:02d}"

CONTRACT_EXTS = {".pdf", ".doc", ".docx"}
DELIVERY_EXTS = IMAGE_EXTS | DOC_EXTS


def _date(v: str):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _f(v):
    try:
        return float(v) if v else 0.0
    except ValueError:
        return 0.0


def _form_ctx(db: Session, user: User, contract=None, parsed=None, file_path="", error=""):
    return {
        "active": "contracts", "user": user, "contract": contract,
        "parsed": parsed, "file_path": file_path, "error": error,
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "projects": db.query(Project).order_by(Project.name).all(),
        "categories": [r[0] for r in db.query(PurchaseContract.category)
                        .filter(PurchaseContract.category != "").distinct().all()],
        # 审批留痕：编辑已有合同时带上单据 id，模板底部渲染审批状态条
        "approval_entity": "purchase_contract",
        "approval_id": (contract.id if contract is not None else None),
    }


def _match_or_create_supplier(db: Session, name: str) -> Supplier | None:
    """按名称匹配供应商；匹配不到则自动创建（用于合同乙方自动成为供应商）。"""
    name = (name or "").strip()
    if not name:
        return None
    for s in db.query(Supplier).all():
        if name == s.name or (s.name and (name in s.name or s.name in name)):
            return s
    supplier = Supplier(name=name, note="由采购合同乙方自动创建")
    db.add(supplier)
    db.flush()
    return supplier


@router.get("/contracts")
def contract_list(request: Request, q: str = "", supplier_id: list[int] = Query([]),
                  project_id: list[int] = Query([]), delivery: list[str] = Query([]),
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    query = db.query(PurchaseContract)
    if q:
        like = f"%{q}%"
        query = query.filter((PurchaseContract.name.like(like))
                             | (PurchaseContract.contract_no.like(like))
                             | (PurchaseContract.party_a.like(like))
                             | (PurchaseContract.party_b.like(like))
                             | (PurchaseContract.category.like(like))
                             | (PurchaseContract.supplier_id.in_(
                                 db.query(Supplier.id).filter(Supplier.name.like(like)))))
    if supplier_id:
        query = query.filter(PurchaseContract.supplier_id.in_(supplier_id))
    if -1 in project_id:
        _others = [x for x in project_id if x != -1]
        if _others:
            query = query.filter((PurchaseContract.project_id.is_(None)) | (PurchaseContract.project_id.in_(_others)))
        else:
            query = query.filter(PurchaseContract.project_id.is_(None))
    elif project_id:
        query = query.filter((PurchaseContract.project_id.in_(project_id)) | (PurchaseContract.project_id.is_(None)))
    if "delivered" in delivery:
        query = query.filter(PurchaseContract.delivery_notes.any())
    elif "undelivered" in delivery:
        query = query.filter(~PurchaseContract.delivery_notes.any())
    contracts = query.order_by(PurchaseContract.sign_date.desc(),
                               PurchaseContract.id.desc()).all()
    return render(request, "contracts/list.html", {
        "active": "contracts", "user": user, "contracts": contracts, "q": q,
        "supplier_id": supplier_id, "project_id": project_id, "delivery": delivery,
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "projects": db.query(Project).order_by(Project.name).all(),
    })


@router.get("/contracts/new")
def contract_new(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    ctx = _form_ctx(db, user)
    ctx["default_contract_no"] = _default_contract_no(db)
    return render(request, "contracts/form.html", ctx)


def _match_supplier(db: Session, name: str):
    """按名称匹配供应商（精确 → 包含）。"""
    if not name:
        return None
    sup = db.query(Supplier).filter(Supplier.name == name).first()
    if sup:
        return sup
    for s in db.query(Supplier).all():
        if name in s.name or s.name in name:
            return s
    return None


@router.post("/contracts/parse")
async def contract_parse(request: Request, file: UploadFile | None = File(None),
                         db: Session = Depends(get_db), user: User = Depends(login_required)):
    """上传合同文件（PDF/Word）→ 识别合同信息与时间节点 → 预填表单并存档。"""
    ctx = _form_ctx(db, user)
    # file 声明成 None 才收得到「没选文件就点提交」的请求；写成 File(...) 的话
    # FastAPI 会在进函数前回 422 原始 JSON，用户只看到 ["body","file"] 这种报错。
    if file is None or not (file.filename or "").strip():
        ctx["error"] = "请选择合同文件"
        return render(request, "contracts/form.html", ctx)
    data = await file.read()
    file.file.seek(0)
    file_path = await save_upload(file, "contract", allowed=CONTRACT_EXTS)
    if not file_path:
        ctx["error"] = "文件保存失败（支持 PDF / Word，不超过 20MB）"
        return render(request, "contracts/form.html", ctx)
    ctx["file_path"] = file_path

    parsed, parse_err = recognize_contract_file(file.filename, data)
    if parsed:
        ctx["parsed"] = parsed
        ctx["match"] = {"supplier": _match_supplier(db, parsed.get("party_b", ""))}
    else:
        ctx["error"] = (parse_err or "未能识别该合同文件（可能是扫描件或格式特殊）"
                        ).rstrip("。") + "；文件已存档。"
    return render(request, "contracts/form.html", ctx)


@router.post("/api/contracts/parse-json")
async def contract_parse_json(file: UploadFile | None = File(None),
                              db: Session = Depends(get_db),
                              user: User = Depends(login_required)):
    """上传合同文件并返回识别结果 JSON（供飞书等自动化客户端调用）。

    与 /contracts/parse 共用同一套识别逻辑，区别只在返回 JSON 而非预填表单页。
    本接口不建单、不改任何业务数据；文件照常存档到 uploads/contract/。
    """
    if file is None or not (file.filename or "").strip():
        return JSONResponse({"ok": False, "error": "请选择合同文件（表单字段名 file）"}, status_code=400)

    data = await file.read()
    file.file.seek(0)
    file_path = await save_upload(file, "contract", allowed=CONTRACT_EXTS)
    if not file_path:
        return JSONResponse(
            {"ok": False, "error": "文件保存失败（仅支持 PDF / Word，且不超过 20MB）"},
            status_code=400)

    parsed, parse_err = recognize_contract_file(file.filename, data)

    payload = {
        "ok": bool(parsed),
        "file": {
            "name": file.filename,
            "path": file_path,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        "fields": None,
        "warnings": [],
        "error": parse_err or "",
    }
    if parsed:
        sign_date = parsed.get("sign_date")
        milestones = [
            {"name": m.get("name", ""),
             "m_date": m["m_date"].isoformat() if m.get("m_date") else ""}
            for m in (parsed.get("milestones") or [])
        ]
        supplier = _match_supplier(db, parsed.get("party_b", ""))
        payload["fields"] = {
            "name": parsed.get("name", ""),
            "party_a": parsed.get("party_a", ""),
            "party_b": parsed.get("party_b", ""),
            "amount": float(parsed.get("amount") or 0),
            "category": parsed.get("category", ""),
            "sign_date": sign_date.isoformat() if sign_date else "",
            "milestones": milestones,
            # 合同纸上通常没有该编号，仅作提示，建议由系统自动生成
            "contract_no_hint": parsed.get("contract_no", ""),
            "supplier_match": ({"id": supplier.id, "name": supplier.name}
                               if supplier is not None else None),
            "summary": str(parsed.get("raw_text", "") or "")[:500],
        }
        # 供调用方决定「哪些字段必须人工补」，避免把空值直接写进业务表
        warn = payload["warnings"]
        if not payload["fields"]["sign_date"]:
            warn.append("sign_date_missing")
        if not milestones:
            warn.append("milestones_missing")
        if not payload["fields"]["party_b"]:
            warn.append("party_b_missing")
        if payload["fields"]["amount"] <= 0:
            warn.append("amount_zero")

    return JSONResponse(payload)


def _ensure_material_from_contract_line(db: Session, cm: ContractMaterial) -> None:
    """把合同采购清单行（ContractMaterial）同步进材料管理（Material），保留供应商/项目/合同。
    幂等：本合同已有同名同规格同单位材料则不动（尊重人工改价）；
    未绑定合同的同规格材料优先复用；否则新建一条绑定本合同。"""
    if not cm or not (cm.name or "").strip():
        return
    mat = db.query(Material).filter(
        Material.contract_id == cm.contract_id,
        Material.name == cm.name,
        Material.spec == (cm.spec or ""),
        Material.unit == (cm.unit or ""),
    ).first()
    if mat is None:
        q = db.query(Material).filter(
            Material.name == cm.name,
            Material.spec == (cm.spec or ""),
            Material.unit == (cm.unit or ""),
            Material.contract_id.is_(None),
        )
        if cm.supplier_id:
            mat = q.filter(Material.supplier_id == cm.supplier_id).first()
        if mat is None:
            mat = q.first()
    if mat is not None:
        if not mat.contract_id:
            mat.contract_id = cm.contract_id
        if not mat.project_id:
            mat.project_id = cm.project_id
        if not mat.supplier_id:
            mat.supplier_id = cm.supplier_id
        if not mat.ref_price and cm.unit_price:
            mat.ref_price = round(cm.unit_price, 6)
        if not mat.brand:
            mat.brand = cm.brand or ""
        return
    db.add(Material(
        name=cm.name, spec=cm.spec or "", brand=cm.brand or "",
        unit=cm.unit or "件", ref_price=round(cm.unit_price or 0, 6),
        supplier_id=cm.supplier_id, project_id=cm.project_id,
        contract_id=cm.contract_id, note="由合同清单自动导入",
    ))
    db.flush()


@router.post("/contracts/new")
def contract_create(request: Request,
                    contract_no: str = Form(""), name: str = Form(...),
                    supplier_name: str = Form(""),
                    supplier_id: int = Form(0), project_id: int = Form(0),
                    no_delivery: str = Form(""),
                    party_a: str = Form(""),
                    party_b: str = Form(""), amount: float = Form(0),
                    category: str = Form(""),
                    a_tax_no: str = Form(""), a_address: str = Form(""),
                    a_contact: str = Form(""), a_bank: str = Form(""), a_account: str = Form(""),
                    b_tax_no: str = Form(""), b_bank: str = Form(""), b_account: str = Form(""),
                    sign_date: str = Form(""), file_path: str = Form(""),
                    note: str = Form(""),
                    ms_name: list[str] = Form([]), ms_date: list[str] = Form([]),
                    ms_amount: list[str] = Form([]),
                    cm_name: list[str] = Form([]), cm_spec: list[str] = Form([]),
                    cm_unit: list[str] = Form([]), cm_quantity: list[str] = Form([]),
                    cm_unit_price: list[str] = Form([]), cm_amount: list[str] = Form([]),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    contract = PurchaseContract(contract_no=contract_no, name=name,
                                supplier_id=supplier_id or None,
                                project_id=project_id or None,
                                no_delivery=bool(no_delivery),
                                party_a=party_a, party_b=party_b, amount=amount,
                                category=category,
                                a_tax_no=a_tax_no, a_address=a_address,
                                a_contact=a_contact, a_bank=a_bank, a_account=a_account,
                                b_tax_no=b_tax_no, b_bank=b_bank, b_account=b_account,
                                sign_date=_date(sign_date), file_path=file_path, note=note)
    # 未选供应商时，优先按供应商搜索框名称匹配；再用乙方名称匹配/创建供应商
    if not supplier_id and supplier_name:
        _sn = supplier_name.strip()
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                supplier_id = _s.id
                contract.supplier_id = supplier_id
                break
    if not supplier_id and party_b:
        supplier = _match_or_create_supplier(db, party_b)
        if supplier:
            contract.supplier_id = supplier.id
    if not contract_no.strip():
        contract.contract_no = _default_contract_no(db)
    db.add(contract)
    db.flush()
    for i, mn in enumerate(ms_name):
        mn = (mn or "").strip()
        if not mn:
            continue
        d = _date(ms_date[i]) if i < len(ms_date) else None
        amt = _f(ms_amount[i]) if i < len(ms_amount) else 0.0
        if d:
            db.add(ContractMilestone(contract_id=contract.id, name=mn, m_date=d, amount=amt))
    _cm_list = []
    for i, cn in enumerate(cm_name):
        cn = (cn or "").strip()
        if not cn:
            continue
        _cm_list.append(ContractMaterial(
            contract_id=contract.id, name=cn,
            spec=(cm_spec[i] if i < len(cm_spec) else "") or "",
            unit=(cm_unit[i] if i < len(cm_unit) else "") or "件",
            quantity=_f(cm_quantity[i]) if i < len(cm_quantity) else 0.0,
            unit_price=_f(cm_unit_price[i]) if i < len(cm_unit_price) else 0.0,
            amount=_f(cm_amount[i]) if i < len(cm_amount) else 0.0,
            supplier_id=contract.supplier_id,   # 默认材料供应商=合同乙方供应商
            project_id=contract.project_id,     # 默认材料项目=合同所属项目
        ))
    db.add_all(_cm_list)
    db.flush()
    for _cm in _cm_list:
        _ensure_material_from_contract_line(db, _cm)
    db.commit()
    log_audit(db, user, "新增", "采购合同", contract.id, f"{contract.name} {contract.contract_no or ''}".strip())
    return RedirectResponse(url=f"/contracts/{contract.id}", status_code=303)


@router.post("/contracts/{contract_id}/toggle-delivery")
def contract_toggle_delivery(contract_id: int, request: Request,
                             db: Session = Depends(get_db),
                             user: User = Depends(login_required)):
    """切换合同是否需要送货单。"""
    ct = db.get(PurchaseContract, contract_id)
    if ct:
        ct.no_delivery = not ct.no_delivery
        db.commit()
    return RedirectResponse(url=f"/contracts/{contract_id}", status_code=303)


def _contract_detail_ctx(db: Session, user: User, contract_id: int, error: str = "") -> dict:
    """合同详情页完整上下文。

    抽成独立函数是因为 export-word 的两条失败分支要回到同一个页面提示原因；
    早先它们只传 contract/error 三四个变量，detail.html 一渲染就 UndefinedError 500。
    """
    contract = db.get(PurchaseContract, contract_id)
    orders = db.query(PurchaseOrder).filter(
        PurchaseOrder.contract_id == contract_id).order_by(PurchaseOrder.order_date.asc(), PurchaseOrder.id.asc()).all()
    ordered_total = sum(po.total_amount for po in orders)
    remain = round(contract.amount - ordered_total, 2)
    progress = round(ordered_total / contract.amount * 100, 1) if contract.amount else 0
    # 每张采购单占合同比例；关联发票/付款金额（一次性聚合，避免 N+1）
    _ids = [o.id for o in orders]
    _inv_map = dict(db.query(Invoice.po_id, func.coalesce(func.sum(Invoice.amount), 0)).filter(
        Invoice.po_id.in_(_ids), Invoice.status != "作废").group_by(Invoice.po_id).all()) if _ids else {}
    _pay_map = dict(db.query(SupplierPayment.po_id, func.coalesce(func.sum(SupplierPayment.amount), 0)).filter(
        SupplierPayment.po_id.in_(_ids), not_voided(SupplierPayment)).group_by(SupplierPayment.po_id).all()) if _ids else {}
    for o in orders:
        o.percent = round(o.total_amount / contract.amount * 100, 1) if contract.amount else 0
        o.invoice_amt = round(float(_inv_map.get(o.id) or 0), 2)
        o.paid_amt = round(float(_pay_map.get(o.id) or 0), 2)
    # 关联发票与付款
    contract_invoices = db.query(Invoice).filter(
        Invoice.contract_id == contract_id).order_by(Invoice.invoice_date.is_(None), Invoice.invoice_date.asc(), Invoice.id.asc()).all()
    # 作废的付款仍列出来（带作废标记），但不计入上面的已付金额——
    # 财务要能看到「这里有一笔作废的记录」才能对账，直接藏起来反而会漏。
    contract_payments = db.query(SupplierPayment).filter(
        SupplierPayment.contract_id == contract_id).order_by(
        SupplierPayment.pay_date.desc()).all()
    # 发票/付款关联采购单号
    po_map = {p.id: p for p in db.query(PurchaseOrder).all()}
    for i in contract_invoices:
        i.po_no = (po_map[i.po_id].order_no or f"采购单#{i.po_id}") if i.po_id in po_map else ""
    for p in contract_payments:
        p.po_no = (po_map[p.po_id].order_no or f"采购单#{p.po_id}") if p.po_id in po_map else ""
    invoice_total = sum(i.amount for i in contract_invoices if i.status != "作废")
    paid_total = sum(p.amount for p in contract_payments)
    remain_invoice = max(contract.amount - invoice_total, 0) if contract.amount else 0
    unpaid = max(contract.amount - paid_total, 0)
    return {
        "active": "contracts", "user": user, "contract": contract, "error": error,
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "projects": db.query(Project).order_by(Project.name).all(),
        # 审批留痕：合同详情页展示审批状态
        "approval_entity": "purchase_contract",
        "approval_id": contract_id,
        "colors": KIND_COLORS,
        "orders": orders, "ordered_total": ordered_total,
        "ordered_pct": progress,
        "remain": remain,
        "remain_pct": round(max(remain, 0) / contract.amount * 100, 1) if contract.amount else 0,
        "progress": progress,
        "contract_invoices": contract_invoices, "contract_payments": contract_payments,
        "invoice_total": invoice_total,
        "inv_pct": round(invoice_total / contract.amount * 100, 1) if contract.amount else 0,
        "remain_invoice": remain_invoice,
        "remain_inv_pct": round(remain_invoice / contract.amount * 100, 1) if contract.amount else 0,
        "contract_paid_total": paid_total,
        "paid_pct": round(paid_total / contract.amount * 100, 1) if contract.amount else 0,
        "unpaid": unpaid,
        "unpaid_pct": round(unpaid / contract.amount * 100, 1) if contract.amount else 0,
        "materials": db.query(Material).filter(
            Material.contract_id == contract.id).order_by(Material.name).all(),
        "today": date.today(),
    }


@router.get("/contracts/{contract_id}")
def contract_detail(contract_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    contract = db.get(PurchaseContract, contract_id)
    if contract is None:
        return RedirectResponse(url="/contracts", status_code=303)
    return render(request, "contracts/detail.html",
                  _contract_detail_ctx(db, user, contract_id))


@router.get("/contracts/{contract_id}/export-word")
def contract_export_word(contract_id: int, request: Request,
                         db: Session = Depends(get_db),
                         user: User = Depends(login_required)):
    """基于公司采购合同模板（公司资料库「合同模板」类最新 docx）导出 Word。"""
    contract = db.get(PurchaseContract, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="合同不存在")
    tpl = db.query(CompanyDoc).filter(
        CompanyDoc.category == "合同模板",
        CompanyDoc.file_path.like("%.docx")).order_by(CompanyDoc.id.desc()).first()
    if not tpl:
        return render(request, "contracts/detail.html", _contract_detail_ctx(
            db, user, contract_id,
            "请先到「公司资料」上传采购合同模板（Word，类别选合同模板），再导出。"))
    tpl_path = os.path.join(UPLOAD_DIR, tpl.file_path.replace("/uploads/", ""))
    if not os.path.exists(tpl_path):
        return render(request, "contracts/detail.html", _contract_detail_ctx(
            db, user, contract_id, "合同模板文件不存在，请重新上传。"))

    supplier = contract.supplier
    mapping = {
        "{{contract_name}}": contract.name or "",
        "{{contract_no}}": contract.contract_no or "",
        "{{party_a}}": contract.party_a or "",
        "{{party_b}}": contract.party_b or (supplier.name if supplier else ""),
        "{{a_tax_no}}": contract.a_tax_no or "",
        "{{a_address}}": contract.a_address or "",
        "{{a_contact}}": contract.a_contact or "",
        "{{a_bank}}": contract.a_bank or "",
        "{{a_account}}": contract.a_account or "",
        "{{b_tax_no}}": contract.b_tax_no or (supplier.tax_no if supplier else ""),
        "{{b_bank}}": contract.b_bank or (supplier.bank_accounts[0].bank_name if supplier and supplier.bank_accounts else ""),
        "{{b_account}}": contract.b_account or (supplier.bank_accounts[0].account_no if supplier and supplier.bank_accounts else ""),
        "{{amount}}": f"{contract.amount:,.2f}" if contract.amount else "",
        "{{sign_date}}": str(contract.sign_date) if contract.sign_date else "",
        "{{project}}": contract.project.name if contract.project else "",
        "{{note}}": contract.note or "",
    }

    doc = Document(tpl_path)

    def _replace_para(para):
        for k, v in mapping.items():
            if k in para.text:
                para.text = para.text.replace(k, v)

    for para in doc.paragraphs:
        _replace_para(para)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    _replace_para(para)
        # 采购清单表填充：表头含“名称/材料”且“数量/单价”
        if contract.materials and len(table.rows) >= 1:
            header = [c.text.strip() for c in table.rows[0].cells]
            has_name = any(("名称" in h or "材料" in h) for h in header)
            has_qty = any(("数量" in h or "单价" in h) for h in header)
            if has_name and has_qty:
                def _col(col):
                    for i, h in enumerate(header):
                        if col in h:
                            return i
                    return None
                i_name, i_spec, i_unit = _col("名称") or _col("材料"), _col("规格"), _col("单位")
                i_qty, i_price, i_amt = _col("数量"), _col("单价"), _col("金额") or _col("小计")
                for m in contract.materials:
                    cells = table.add_row().cells
                    if i_name is not None:
                        cells[i_name].text = m.name
                    if i_spec is not None:
                        cells[i_spec].text = m.spec or ""
                    if i_unit is not None:
                        cells[i_unit].text = m.unit or ""
                    if i_qty is not None:
                        cells[i_qty].text = f"{m.quantity:g}" if m.quantity else ""
                    if i_price is not None:
                        cells[i_price].text = f"{m.unit_price:,.2f}" if m.unit_price else ""
                    if i_amt is not None:
                        cells[i_amt].text = f"{m.amount:,.2f}" if m.amount else ""

    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    doc.save(tmp.name)
    tmp.close()
    fname = f"采购合同-{contract.name or contract.id}.docx"
    return FileResponse(tmp.name, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        filename=fname)


@router.get("/contracts/{contract_id}/edit")
def contract_edit(contract_id: int, request: Request,
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    contract = db.get(PurchaseContract, contract_id)
    if contract is None:
        return RedirectResponse(url="/contracts", status_code=303)
    ctx = _form_ctx(db, user, contract=contract, file_path=contract.file_path)
    return render(request, "contracts/form.html", ctx)


@router.post("/contracts/{contract_id}/edit")
def contract_update(contract_id: int, request: Request,
                    contract_no: str = Form(""), name: str = Form(...),
                    no_delivery: str = Form(""),
                    supplier_name: str = Form(""),
                    supplier_id: int = Form(0), project_id: int = Form(0),
                    party_a: str = Form(""),
                    party_b: str = Form(""), amount: float = Form(0),
                    category: str = Form(""),
                    a_tax_no: str = Form(""), a_address: str = Form(""),
                    a_contact: str = Form(""), a_bank: str = Form(""), a_account: str = Form(""),
                    b_tax_no: str = Form(""), b_bank: str = Form(""), b_account: str = Form(""),
                    sign_date: str = Form(""), file_path: str = Form(""),
                    note: str = Form(""),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    contract = db.get(PurchaseContract, contract_id)
    if contract is None:
        return RedirectResponse(url="/contracts", status_code=303)
    contract.contract_no = contract_no
    contract.name = name
    contract.no_delivery = bool(no_delivery)
    if not supplier_id and supplier_name:
        _sn = supplier_name.strip()
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                supplier_id = _s.id
                break
    contract.supplier_id = supplier_id or None
    contract.project_id = project_id or None
    contract.party_a = party_a
    contract.party_b = party_b
    contract.amount = amount
    contract.category = category
    contract.a_tax_no = a_tax_no
    contract.a_address = a_address
    contract.a_contact = a_contact
    contract.a_account = a_account
    contract.b_tax_no = b_tax_no
    contract.b_bank = b_bank
    contract.b_account = b_account
    contract.sign_date = _date(sign_date)
    contract.file_path = file_path
    contract.note = note
    db.commit()
    log_audit(db, user, "修改", "采购合同", contract.id, f"{contract.name} {contract.contract_no or ''}".strip())
    return RedirectResponse(url=f"/contracts/{contract_id}", status_code=303)


@router.post("/contracts/{contract_id}/delete")
def contract_delete(contract_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    contract = db.get(PurchaseContract, contract_id)
    if contract:
        _cname = f"{contract.name} {contract.contract_no or ''}".strip()
        # 级联删除该合同下全部采购单（含明细，避免孤儿数据）
        for po in db.query(PurchaseOrder).filter(
                PurchaseOrder.contract_id == contract_id).all():
            db.delete(po)
        db.delete(contract)
        db.commit()
        log_audit(db, user, "删除", "采购合同", contract_id, _cname)
    return RedirectResponse(url="/procurement?tab=contracts", status_code=303)


# ---------- 时间节点 ----------
@router.get("/contracts/milestones/new")
def milestone_new(request: Request, contract_id: int = 0,
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    contracts = db.query(PurchaseContract).order_by(PurchaseContract.contract_no.desc(),
                                                    PurchaseContract.id.desc()).all()
    _r = render(request, "contracts/milestone_form.html", {
        "active": "contracts", "user": user, "contracts": contracts,
        "prefill_contract_id": contract_id,
    })
    _r.headers["Cache-Control"] = "no-store"
    return _r


@router.post("/contracts/milestones/new")
def milestone_create_page(request: Request, contract_id: int = Form(...),
                          name: str = Form(...), m_date: str = Form(""),
                          amount: float = Form(0), note: str = Form(""),
                          back_url: str = Form(""),
                          db: Session = Depends(get_db), user: User = Depends(login_required)):
    db.add(ContractMilestone(contract_id=contract_id, name=name, m_date=_date(m_date),
                             amount=amount, note=note))
    db.commit()
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url=f"/contracts/{contract_id}#milestones", status_code=303)



def milestone_create(contract_id: int, request: Request,
                     name: str = Form(...), m_date: str = Form(...),
                     amount: float = Form(0), note: str = Form(""),
                     db: Session = Depends(get_db), user: User = Depends(login_required)):
    db.add(ContractMilestone(contract_id=contract_id, name=name, m_date=_date(m_date),
                             amount=amount, note=note))
    db.commit()
    return RedirectResponse(url=f"/contracts/{contract_id}#milestones", status_code=303)


@router.post("/contracts/milestones/{ms_id}/edit")
def milestone_edit(ms_id: int, request: Request,
                   name: str = Form(...), m_date: str = Form(...),
                   amount: float = Form(0), note: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    """编辑合同时间节点。"""
    ms = db.get(ContractMilestone, ms_id)
    cid = ms.contract_id if ms else 0
    if ms:
        ms.name = name
        ms.m_date = _date(m_date)
        ms.amount = amount
        ms.note = note
        db.commit()
    return RedirectResponse(url=f"/contracts/{cid}", status_code=303)


@router.post("/contracts/milestones/{ms_id}/delete")
def milestone_delete(ms_id: int, request: Request,
                     db: Session = Depends(get_db), user: User = Depends(login_required)):
    ms = db.get(ContractMilestone, ms_id)
    cid = ms.contract_id if ms else 0
    if ms:
        db.delete(ms)
        db.commit()
    return RedirectResponse(url=f"/contracts/{cid}#milestones", status_code=303)


@router.post("/contracts/materials/{cm_id}/edit")
def contract_material_update(cm_id: int, request: Request,
                             name: str = Form(...), spec: str = Form(""),
                             unit: str = Form("件"), quantity: float = Form(0),
                             unit_price: float = Form(0), amount: float = Form(0),
                             supplier_name: str = Form(""),
                             supplier_id: int = Form(0), project_id: int = Form(0),
                             db: Session = Depends(get_db), user: User = Depends(login_required)):
    cm = db.get(ContractMaterial, cm_id)
    cid = cm.contract_id if cm else 0
    if cm:
        cm.name = name
        cm.spec = spec
        cm.unit = unit or "件"
        cm.quantity = quantity
        cm.unit_price = unit_price
        cm.amount = amount
        if not supplier_id and supplier_name:
            _sn = supplier_name.strip()
            for _s in db.query(Supplier).all():
                if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                    supplier_id = _s.id
                    break
        cm.supplier_id = supplier_id or None
        cm.project_id = project_id or None
        db.commit()
    return RedirectResponse(url=f"/contracts/{cid}#materials", status_code=303)


@router.post("/contracts/{contract_id}/materials/select")
def contract_material_select(contract_id: int, request: Request,
                             material_ids: list[int] = Form([]),
                             quantities: list[float] = Form([]),
                             db: Session = Depends(get_db),
                             user: User = Depends(login_required)):
    """从材料管理库勾选材料，批量添加为合同采购清单。"""
    contract = db.get(PurchaseContract, contract_id)
    for i, mid in enumerate(material_ids):
        mat = db.get(Material, mid)
        if mat is None:
            continue
        qty = quantities[i] if i < len(quantities) else 1
        price = mat.ref_price or 0
        cm = db.query(ContractMaterial).filter(
            ContractMaterial.contract_id == contract_id,
            ContractMaterial.name == mat.name,
            ContractMaterial.spec == (mat.spec or ""),
            ContractMaterial.unit == (mat.unit or "件"),
        ).order_by(ContractMaterial.id).first()
        if cm:
            cm.brand = mat.brand or cm.brand
            cm.quantity = qty
            cm.unit_price = price
            cm.amount = round(qty * price, 2)
            cm.supplier_id = contract.supplier_id if contract else None
        else:
            db.add(ContractMaterial(
                contract_id=contract_id, name=mat.name, spec=mat.spec, brand=mat.brand,
                unit=mat.unit or "件", quantity=qty, unit_price=price,
                amount=round(qty * price, 2),
                supplier_id=contract.supplier_id if contract else None))
    db.commit()
    return RedirectResponse(url=f"/contracts/{contract_id}#materials", status_code=303)


@router.post("/contracts/{contract_id}/materials/set-project")
def contract_materials_set_project(contract_id: int, request: Request,
                                  project_id: int = Form(0),
                                  db: Session = Depends(get_db),
                                  user: User = Depends(login_required)):
    """一键把该合同全部清单材料设置为同一所属项目（同一合同通常属于同一项目）。"""
    db.query(ContractMaterial).filter(
        ContractMaterial.contract_id == contract_id).update(
        {ContractMaterial.project_id: project_id or None})
    db.commit()
    return RedirectResponse(url=f"/contracts/{contract_id}#materials", status_code=303)


# ---------- 送货单 ----------

@router.get("/contracts/delivery-notes/new")
def delivery_note_new(request: Request, contract_id: int = 0,
                      db: Session = Depends(get_db),
                      user: User = Depends(login_required)):
    contracts = db.query(PurchaseContract).order_by(PurchaseContract.contract_no.desc(),
                                                    PurchaseContract.id.desc()).all()
    projects = db.query(Project).order_by(Project.name).all()
    pos = db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).all()
    invoices = db.query(Invoice).filter(Invoice.status != "作废").order_by(
        Invoice.invoice_date.desc()).all()
    # 登记付款时选关联单据：作废的付款不该再被新付款挂到同一张单上
    payments = db.query(SupplierPayment).filter(not_voided(SupplierPayment)).order_by(
        SupplierPayment.pay_date.desc()).all()
    ctx = {
        "active": "contracts", "user": user, "contracts": contracts, "projects": projects,
        "pos": pos, "invoices": invoices, "payments": payments,
        "prefill_contract_id": contract_id,
    }
    if contract_id:
        _pc = db.get(PurchaseContract, contract_id)
        if _pc:
            ctx["prefill_project_id"] = _pc.project_id or 0
    _r = render(request, "contracts/delivery_form.html", ctx)
    _r.headers["Cache-Control"] = "no-store"
    return _r


@router.post("/contracts/delivery-notes/new")
async def delivery_note_create_page(request: Request,
                                    contract_id: int = Form(0), project_id: int = Form(0),
                                    po_id: int = Form(0), invoice_id: int = Form(0),
                                    payment_id: int = Form(0),
                                    note_no: str = Form(""), delivery_date: str = Form(""),
                                    quantity: float = Form(0), amount: float = Form(0),
                                    note: str = Form(""),
                                    file: UploadFile | None = File(None),
                                    back_url: str = Form(""),
                                    db: Session = Depends(get_db),
                                    user: User = Depends(login_required)):
    """独立新增送货单（自动关联合同/项目，手动选采购单/发票/付款）。"""
    file_path = ""
    if file and file.filename:
        file_path = await save_upload(file, "delivery", allowed={".pdf", ".docx", ".jpg", ".jpeg", ".png"}) or ""
    dn = DeliveryNote(contract_id=contract_id or None if contract_id else 0,
                      project_id=project_id or None,
                      po_id=po_id or None, invoice_id=invoice_id or None,
                      payment_id=payment_id or None,
                      note_no=note_no, delivery_date=_date(delivery_date),
                      quantity=quantity, amount=amount, file_path=file_path, note=note)
    db.add(dn)
    db.commit()
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url="/contracts", status_code=303)


@router.post("/contracts/{contract_id}/delivery-notes")
async def delivery_note_create(contract_id: int, request: Request,
                               note_no: str = Form(""), delivery_date: str = Form(""),
                               quantity: float = Form(0), amount: float = Form(0),
                               file: UploadFile | None = File(None), note: str = Form(""),
                               db: Session = Depends(get_db),
                               user: User = Depends(login_required)):
    file_path = await save_upload(file, "delivery", allowed=DELIVERY_EXTS)
    db.add(DeliveryNote(contract_id=contract_id, note_no=note_no,
                        delivery_date=_date(delivery_date), quantity=quantity,
                        amount=amount, file_path=file_path or "", note=note))
    db.commit()
    return RedirectResponse(url=f"/contracts/{contract_id}#delivery", status_code=303)


@router.post("/contracts/delivery-notes/{note_id}/delete")
def delivery_note_delete(note_id: int, request: Request,
                         db: Session = Depends(get_db), user: User = Depends(login_required)):
    note = db.get(DeliveryNote, note_id)
    cid = note.contract_id if note else 0
    if note:
        db.delete(note)
        db.commit()
    return RedirectResponse(url=f"/contracts/{cid}#delivery", status_code=303)


# ---------- 工程管理（阶段矩阵 + 甘特图） ----------
#
# 这个页面改版前叫「工期规划」，三个模式（时间线 / 工期规划 / 看板），
# 主语是**采购合同的节点**。问题不在于样式，在于主语错了——合同节点不是工程进度。
# 实测（2026-10，本地库 48 份采购合同 / 33 个里程碑）：
#   · 时间线 4717px 高、48 个事件**全部已过期**（最晚 2026-09-28，今天 2026-10-18），
#     未来 0 条。整页就是让人滚 4700 像素看两年前的历史。
#   · 甘特图跨 963 天画进 11786px，可视区 950px，要横滚 12 屏；
#     46 个节点渲染宽度**全是 0px**，右半边整片空白。
#   · 而真正的工程进度 project_phases（17 条、7 个标准阶段）这个路由从来没读过。
#
# 现在改成两个视图，主语换成「项目 × 施工阶段」：
#   matrix  阶段看板（默认）：行=项目，列=阶段，一格一个状态，点一下就能改。
#   gantt   甘特图：单项目 + 近端窗口，阶段当骨架，任务挂在阶段下。
#
# 「时间线」被合并掉了：它和甘特图是同一份数据的两种画法，而它真正有价值的部分
# （接下来有什么）由甘特图的近端窗口和「近期交货节点」承担。

PHASE_ORDER = ["拆改", "拆除", "水电", "泥瓦", "泥木", "木工",
               "油漆", "安装", "验收", "清洁验收", "竣工", "售后"]
PHASE_STATUS = ["未开始", "进行中", "已完成"]
# 存量数据里还有两种别名：项目 3 的阶段写成「施工中」，自动生成的阶段写成
# 「待开始」。读的时候归一化到 PHASE_STATUS，写的时候只写归一化后的词。
# 不做数据迁移——改写用户已有数据比多一个别名危险得多。
PHASE_ALIAS = {"施工中": "进行中", "待开始": "未开始"}
# 甘特图窗口。8 周是装修项目的常见阶段跨度：一个 2-3 个月的项目正好一屏。
GANTT_RANGES = {"4w": 28, "8w": 56, "12w": 84}
# 时间轴缩放：range 决定「看多远」，zoom 决定「一天画多宽」——两件事正交。
#   100% = 适配：轨道铺满卡片，刻度按周（改版后一直以来的样子）；
#   150% / 220% = 给每天一个最小宽度（24 / 36px），放大后横向滚动、刻度细到天。
# 放大档位用「每天多少像素」而不是「容器宽度的倍数」：按倍数放大时，
# 1200 宽的屏上 12 周窗口只有 12px/天，逐日刻度会糊成一串数字（实测过）。
GANTT_ZOOMS = [100, 150, 220]
GANTT_ZOOM_PX = {100: 0, 150: 24, 220: 36}


def _norm_phase_status(raw: str | None) -> str:
    """阶段状态归一化。认不出来的词按「未开始」处理，不抛异常。"""
    s = (raw or "").strip()
    s = PHASE_ALIAS.get(s, s)
    return s if s in PHASE_STATUS else "未开始"


def _task_bar(task, span_start: date, total_days: int):
    """任务 → 甘特条的位置与宽度（百分比）。完全落在窗口外返回 None。

    窗口化的意义就在这里：改版前不裁剪，963 天的窗口里一条 3 天的任务
    宽度是 0.3% × 11786px ≈ 35px，但节点（里程碑）连 1px 都不到。
    裁到近端窗口后，同样的任务占 3/56 ≈ 5.4%，看得见。
    """
    s = task.start_date
    e = task.end_date or task.start_date
    if not s or not e:
        return None
    if e < span_start or s > span_start + timedelta(days=total_days):
        return None
    ps = max((s - span_start).days, 0)
    pe = min((e - span_start).days, total_days)
    if pe < ps:
        return None
    left = ps / total_days * 100
    width = max((pe - ps) / total_days * 100, 1.2)   # 至少 1.2%，否则细得看不见
    return {"left": round(left, 2),
            "width": round(min(width, max(100 - left, 1.2)), 2),
            "clip_l": s < span_start, "clip_r": e > span_start + timedelta(days=total_days)}


def _task_days(task) -> int | None:
    """工期天数，含头含尾：10-20 到 10-20 是 1 天。

    日期没填就返回 None，模板里那一格留空——排期表上写「0 天」比不写更费解。
    """
    s = task.start_date
    if not s:
        return None
    e = task.end_date or s
    return max((e - s).days + 1, 1)


def _axis(span_start: date, total_days: int, zoom: int = 100) -> list[dict]:
    """时间轴刻度。缩放 > 100% 时细到每天，否则按周。

    末端的刻度要丢掉：`.gantt-axis-label` 是 `left:pct% + translateX(-50%)`，
    pct=100 时半个标签探出轨道右缘，把 `.gantt-scroll` 撑出 16px 横向滚动条
    ——实测 1116 → 1132。左端的溢出不会产生滚动条（overflow:auto 只往右算），
    所以只裁右侧。
    """
    out = []
    if zoom > 100:
        # 放大后一天至少 15px，「13 14 15…」逐日写才读得出来；
        # 每月 1 号把日号换成「11月」，否则连着 30 个数字分不清月份。
        for i in range(total_days + 1):
            d = span_start + timedelta(days=i)
            pct = round(i / total_days * 100, 2)
            if pct > 97:
                break
            out.append({"date": d, "pct": pct,
                        "label": f"{d.month}月" if d.day == 1 else str(d.day),
                        "edge": "edge-start" if pct < 3 else ""})
        return out

    d = span_start
    while d <= span_start + timedelta(days=total_days):
        pct = round((d - span_start).days / total_days * 100, 2)
        if pct <= 97:
            # 首尾两个刻度要改成「左对齐 / 右对齐」，否则 translateX(-50%)
            # 会把它们各裁掉一半：左端被滚动容器切掉（不产生滚动条，但字少了
            # 一半），右端把容器撑宽 16px。中间的正常居中。
            edge = "edge-start" if pct < 3 else ""
            out.append({"date": d, "pct": pct,
                        "label": f"{d.month:02d}-{d.day:02d}", "edge": edge})
        d += timedelta(days=7)
    return out


def _matrix_rows(projects, tasks, today: date) -> tuple[list[str], list[dict]]:
    """项目 × 阶段矩阵。

    列 = 所有项目出现过的阶段名，按 PHASE_ORDER 排序，认不出来的排在后面。
    **不能写死 7 个标准阶段**：项目 3 用的是「拆除 / 水电 / 泥木」三个自定义阶段，
    写死 7 列会让它整行落空，看起来像「这个项目没排期」——而它其实在施工中。
    """
    col_names: list[str] = []
    for p in projects:
        for ph in sorted(p.phases, key=lambda x: (x.sort_order, x.id)):
            if ph.name not in col_names:
                col_names.append(ph.name)
    col_names.sort(key=lambda n: (PHASE_ORDER.index(n) if n in PHASE_ORDER else 99, n))

    # 任务按 (项目, 阶段) 归堆，格子里显示「这个阶段还有几条活」
    task_map: dict[tuple, list] = {}
    for t in tasks:
        task_map.setdefault((t.project_id, t.phase_id), []).append(t)

    rows = []
    for p in projects:
        by_name: dict[str, object] = {}
        for ph in sorted(p.phases, key=lambda x: (x.sort_order, x.id)):
            by_name.setdefault(ph.name, ph)     # 同名阶段只取第一个，不重复占格
        cells = []
        for name in col_names:
            ph = by_name.get(name)
            if ph is None:
                cells.append({"name": name, "phase": None, "status": "",
                              "tone": "idle", "raw": "", "tasks": [], "missing": True})
                continue
            st = _norm_phase_status(ph.status)
            cells.append({
                "name": name, "phase": ph, "status": st,
                "raw": ph.status or "",
                "tone": STATE_TONE_MAP.get(st, "idle"),
                "tasks": task_map.get((p.id, ph.id), []),
                "missing": False,
            })
        done = sum(1 for c in cells if c["status"] == "已完成")
        total = sum(1 for c in cells if not c["missing"])
        # 项目级逾期：有竣工日、已过、阶段没做完。阶段本身没有日期字段，
        # 无法判断「单个阶段」逾期，所以逾期只标在项目这一行上。
        overdue = 0
        if p.end_date and p.end_date < today and (total == 0 or done < total):
            overdue = (today - p.end_date).days
        rows.append({"project": p, "cells": cells, "done": done,
                     "total": total, "overdue": overdue})
    return col_names, rows


def _deliveries(db: Session, today: date) -> list[dict]:
    """近期交货节点。

    contract_milestones 的名字是合同 OCR 出来的**自由文本**——实测 33 条里
    17 条都叫「交货期限」，还有「乙方货到工地安装完成」「乙方应在合同签订后
    运输货物」这种整句话。把它当时间线主数据只能制造噪音。
    但按类型筛出「交货」、再限定在近端窗口，它就变成真正卡施工的那件事：
    **材料不到，工人就窝工。** 这就是它该在的位置——一个提醒，不是一条时间线。

    窗口取 [-30, +14] 天：往前 30 天是为了让「该到没到」的节点留在视野里
    （它们才是要催的），往后 14 天是提前量。
    """
    lo, hi = today - timedelta(days=30), today + timedelta(days=14)
    out = []
    q = (db.query(ContractMilestone)
           .filter(ContractMilestone.m_date.isnot(None),
                   ContractMilestone.m_date >= lo,
                   ContractMilestone.m_date <= hi)
           .order_by(ContractMilestone.m_date))
    for ms in q:
        if milestone_kind(ms.name) != "交货":
            continue
        c = ms.contract
        out.append({
            "date": ms.m_date,
            "days": (ms.m_date - today).days,
            "name": c.name if c else "",
            "contract_id": c.id if c else 0,
            "supplier": (c.supplier.name if c and c.supplier else ""),
            "project": (c.project.name if c and c.project else ""),
            "node": ms.name,
        })
    return out


def _gantt_payload(db: Session, project: Project, tasks, today: date,
                   rng: str, zoom: int = 100) -> dict:
    """单项目甘特图。窗口 = 今天前后各 N 周 ∪ 本项目已有工期，总长不超过 26 周。"""
    weeks = GANTT_RANGES.get(rng, 56)
    p_tasks = [t for t in tasks if t.project_id == project.id]

    # 前后对称。原来只往前留 7 天，结果是「已经干完的活」永远在窗口外——
    # 项目没填 start_date 时尤其明显（存量项目一个都没填），甘特图只剩未来，
    # 没法回答「这活干到哪了」。
    span_start = today - timedelta(days=weeks)
    span_end = today + timedelta(days=weeks)
    if project.start_date and project.start_date < span_start:
        span_start = project.start_date
    if project.end_date and project.end_date > span_end:
        span_end = project.end_date

    # 没填开工日时用本项目最早的任务兜底，否则存量项目的历史依旧不可见。
    # 这里接受「新增一条更早的任务会让窗口左移」：项目视图本来就要看全生命周期，
    # 和全局图那种「怕每次编辑都重排」不是一回事。真正防爆的是下面 26 周上限。
    first_task = min((t.start_date for t in p_tasks if t.start_date), default=None)
    if first_task and first_task < span_start:
        span_start = first_task

    # 兜底：项目工期填成两年（存量数据里真有），窗口会再爆成横滚十几屏。
    # 26 周是「一屏半」，再长就该靠切项目来看，不是靠横滚。
    # 超长时**裁历史而不是裁未来**：明天的活必须看得见，去年的可以滚掉。
    if (span_end - span_start).days > 182:
        span_start = span_end - timedelta(days=182)
    total_days = max((span_end - span_start).days, 1)

    by_phase: dict[int, list] = {}
    for t in p_tasks:
        if t.phase_id:
            by_phase.setdefault(t.phase_id, []).append(t)

    # 阶段当骨架：每个阶段一节，节下是它的任务
    sections = []
    for ph in sorted(project.phases, key=lambda x: (x.sort_order, x.id)):
        ts = sorted(by_phase.get(ph.id, []),
                    key=lambda t: (t.start_date or date.max, t.id))
        bars = []
        for t in ts:
            b = _task_bar(t, span_start, total_days)
            bars.append({"task": t, "bar": b, "days": _task_days(t)})
        st = _norm_phase_status(ph.status)
        sections.append({
            "phase": ph, "status": st,
            "tone": STATE_TONE_MAP.get(st, "idle"),
            # 键名不能叫 items：Jinja 里 `sec.items` 永远解析成 dict 的
            # items() 方法，取不到这个键，报「'builtin_function_or_method'
            # object is not iterable」。这个坑只在模板里踩，Python 侧
            # sec["items"] 是好的——所以单元测试查不出来。
            "rows": bars,
            "done": sum(1 for x in ts if (x.status or "") == "已完成"),
            "count": len(ts),
        })

    # 未挂阶段的任务单独一节——存量 2 条任务的 phase_id 是 NULL，
    # 不能因为没归位就从图上消失。
    loose = sorted([t for t in p_tasks if not t.phase_id],
                   key=lambda t: (t.start_date or date.max, t.id))
    loose_items = [{"task": t, "bar": _task_bar(t, span_start, total_days),
                    "days": _task_days(t)} for t in loose]

    # 合同交货 / 验收节点：作为参考线，不占独立行
    nodes = []
    for c in db.query(PurchaseContract).filter(PurchaseContract.project_id == project.id):
        for ms in c.milestones:
            if not ms.m_date or not (span_start <= ms.m_date <= span_end):
                continue
            kind = milestone_kind(ms.name)
            if kind not in ("交货", "验收"):
                continue
            nodes.append({
                "date": ms.m_date, "kind": kind, "name": ms.name,
                "contract": c.name,
                "pct": round((ms.m_date - span_start).days / total_days * 100, 2),
                "color": KIND_COLORS.get(kind, "#6b7280"),
            })
    nodes.sort(key=lambda n: n["date"])

    # 窗口外还有几条任务——不是「还有 N 个阶段的任务」，那个说法会让人
    # 以为窗口里没显示的任务都属于别的阶段。实测有项目一条任务都没有，
    # 这时要明说「还没排期」，而不是含糊地提示「切近 12 周看更远」。
    in_window = sum(1 for s in sections for r in s["rows"] if r["bar"])
    in_window += sum(1 for r in loose_items if r["bar"])

    # 放大档位给轨道一个最小宽度，保证「一天」真的画得下刻度。
    # 210 是左栏宽度，和 .gantt-label 的 width 保持一致。
    zoom_px = GANTT_ZOOM_PX.get(zoom, 0)
    min_css = f"{210 + round(total_days * zoom_px)}px" if zoom_px else "max(720px, 100%)"

    return {
        "project": project,
        "sections": sections,
        "loose": loose_items,
        "nodes": nodes,
        "start": span_start, "end": span_end,
        "total_days": total_days,
        "today_pct": round((today - span_start).days / total_days * 100, 2),
        "axis": _axis(span_start, total_days, zoom),
        "zoom": zoom,
        "min_css": min_css,
        "weeks": weeks,
        "task_total": len(p_tasks),
        "in_window": in_window,
        "outside": len(p_tasks) - in_window,
    }


@router.get("/gantt")
def gantt_view(request: Request, view: str = "", mode: str = "",
               project_id: int = 0, range: str = "8w", zoom: int = 100,
               db: Session = Depends(get_db), user: User = Depends(login_required)):
    """工程管理：阶段看板（默认）+ 甘特图。

    `mode` 是改版前的参数名，保留只为兼容书签和侧边栏里的老链接：
      mode=gantt            → view=gantt
      mode=timeline/board   → view=matrix（时间线和看板都已并入矩阵）
    """
    if not view:
        view = "gantt" if mode == "gantt" else "matrix"
    if view not in ("matrix", "gantt"):
        view = "matrix"
    if range not in GANTT_RANGES:
        range = "8w"
    if zoom not in GANTT_ZOOMS:
        zoom = 100
    zi = GANTT_ZOOMS.index(zoom)
    zoom_prev = GANTT_ZOOMS[zi - 1] if zi > 0 else 0
    zoom_next = GANTT_ZOOMS[zi + 1] if zi < len(GANTT_ZOOMS) - 1 else 0

    today = date.today()
    # 只取在施/待开工的项目排进矩阵：已验收和已流失的项目排在前面
    # 会把真正在干的挤到下面。侧边栏仍能进单个项目。
    projects = (db.query(Project)
                  .filter(Project.status.notin_(["已验收", "已流失"]))
                  .order_by(Project.id).all())
    tasks = db.query(GanttTask).order_by(GanttTask.start_date, GanttTask.id).all()

    col_names, matrix = _matrix_rows(projects, tasks, today)
    deliveries = _deliveries(db, today)

    gantt = None
    if view == "gantt":
        sel = db.get(Project, project_id) if project_id else None
        if sel is None:
            sel = (next((p for p in projects if p.status == "施工中"), None)
                   or (projects[0] if projects else None))
        if sel is not None:
            gantt = _gantt_payload(db, sel, tasks, today, range, zoom)

    # 新建任务弹窗要按项目联动阶段下拉，把「项目 → 阶段」映射一次算好传给前端。
    # 直接在每个 option 上写 data-project 也行，但 3 个项目 × 9 个阶段
    # 就要渲染 27 个 option，而实际只需要当前选中的那一组。
    phase_map = {p.id: [{"id": ph.id, "name": ph.name}
                        for ph in sorted(p.phases, key=lambda x: (x.sort_order, x.id))]
                 for p in projects}

    # 「未完成的任务」清单：矩阵下方一屏就能看完要干什么，
    # 不用切到甘特图找条子。
    open_tasks = [t for t in tasks if (t.status or "未开始") != "已完成"]
    open_tasks.sort(key=lambda t: (t.start_date or date.max, t.id))

    # 存量里有一批 project_id 为空的任务（改版前「看板」模式建的）。
    # 单项目甘特图按 project_id 过滤，矩阵按阶段聚合，两处都捞不到它们——
    # 不单独列出来，这些任务就成了「能建不能删」的黑洞。
    orphans = sorted([t for t in tasks if not t.project_id],
                     key=lambda t: (t.start_date or date.max, t.id))
    orphan_rows = [{"task": t, "days": _task_days(t)} for t in orphans]

    return render(request, "contracts/gantt.html", {
        "active": "gantt", "user": user,
        "view": view, "range": range, "today": today, "zoom": zoom,
        "zoom_prev": zoom_prev, "zoom_next": zoom_next,
        "projects": projects, "phase_map": phase_map,
        "col_names": col_names, "matrix": matrix,
        "deliveries": deliveries,
        "gantt": gantt,
        "open_tasks": open_tasks,
        "orphans": orphans,
        "orphan_rows": orphan_rows,
        # 负责人下拉的候选。**包括已停用的人**（模板里按 active 决定是否显示）：
        # 老任务的负责人可能是已离职的人，只传在职的会让那个值在选择框里消失。
        "staffs": db.query(Staff).order_by(Staff.sort_order, Staff.id).all(),
        "phase_status": PHASE_STATUS,
        "colors": KIND_COLORS,
        "empty": not projects,
    })


# ---------- 工期规划任务 ----------
@router.post("/gantt/tasks")
def gantt_task_create(request: Request,
                      name: str = Form(...), start_date: str = Form(...),
                      end_date: str = Form(""), assignee: str = Form(""),
                      project_id: int = Form(0), phase_id: int = Form(0),
                      note: str = Form(""), back: str = Form("/gantt"),
                      db: Session = Depends(get_db), user: User = Depends(login_required)):
    db.add(GanttTask(name=name, start_date=_date(start_date), end_date=_date(end_date) or _date(start_date),
                     assignee=assignee, project_id=project_id or None,
                     phase_id=phase_id or None, note=note))
    db.commit()
    return RedirectResponse(url=_back_url(back), status_code=303)


def _back_url(back: str) -> str:
    """只接受 /gantt 开头的站内地址。

    任务新建/编辑现在有两个入口（阶段看板、甘特图），提交后该回到哪个视图
    由表单里的 back 字段带过来。**不能直接拿它当跳转目标**——那是开放重定向，
    有人把 back 改成 `//evil.com` 就能把刚登录的用户带走。
    """
    b = (back or "").strip()
    return b if b.startswith("/gantt") and "//" not in b[1:] else "/gantt"


@router.post("/gantt/tasks/{task_id}/edit")
def gantt_task_edit(task_id: int, request: Request,
                    name: str = Form(...), start_date: str = Form(...),
                    end_date: str = Form(""), assignee: str = Form(""),
                    project_id: int = Form(0), phase_id: int = Form(0),
                    note: str = Form(""), back: str = Form("/gantt"),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    """编辑工期任务。"""
    task = db.get(GanttTask, task_id)
    if task:
        task.name = name
        task.start_date = _date(start_date)
        task.end_date = _date(end_date) or _date(start_date)
        task.assignee = assignee
        task.project_id = project_id or None
        task.phase_id = phase_id or None
        task.note = note
        db.commit()
    return RedirectResponse(url=_back_url(back), status_code=303)


@router.post("/gantt/tasks/{task_id}/reschedule")
def gantt_task_reschedule(task_id: int,
                          start_date: str = Form(...), end_date: str = Form(""),
                          db: Session = Depends(get_db), user: User = Depends(login_required)):
    """拖拽改期：只改起止日期，**返回 JSON 而不是 303**。

    为什么不复用 /edit：那个端点收 8 个字段并返回 RedirectResponse。拖拽用
    fetch 调它会把整页 HTML 也拉一遍（白流量 + 白渲染），而且拖一根条不该
    要求前端回填 name / project_id / phase_id —— 少传一个就会把任务改挂到
    别的项目或阶段上。

    只动 start_date / end_date，其余字段一律不碰。调用方必须已在
    app/_route_actions.json 里登记本路由，否则 role_guard 会 fail-closed 403。
    """
    task = db.get(GanttTask, task_id)
    if not task:
        return JSONResponse({"ok": False, "error": "任务不存在或已删除"}, status_code=404)

    start = _date(start_date)
    if not start:
        return JSONResponse({"ok": False, "error": "开始日期无法解析"}, status_code=400)
    end = _date(end_date) or start
    if end < start:
        end = start        # 脏输入不报错，按一天处理；比 500 友好

    task.start_date = start
    task.end_date = end
    db.commit()
    return {"ok": True, "start": start.isoformat(), "end": end.isoformat(),
            "days": (end - start).days + 1}


@router.post("/gantt/tasks/{task_id}/delete")
def gantt_task_delete(task_id: int, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(login_required)):
    task = db.get(GanttTask, task_id)
    if task:
        db.delete(task)
        db.commit()
    return RedirectResponse(url="/gantt", status_code=303)


# ---------- 阶段矩阵：就地改状态 ----------
@router.post("/gantt/phases/{phase_id}/status")
def gantt_phase_status(phase_id: int, request: Request,
                       status: str = Form(...),
                       db: Session = Depends(get_db),
                       user: User = Depends(login_required)):
    """阶段矩阵里点格子改状态，返回 JSON 让前端就地换色，不整页刷新。

    为什么不复用 projects.py 的 `phase_update`：
    那条路由写死了 `url=/projects/{id}` 重定向，而且把认不出的状态词
    一律写成「未开始」——项目 3 的「施工中」阶段只要被它保存一次就变成
    「未开始」，进度凭空倒退。矩阵是高频操作（一天点十几次），
    每次整页刷新也太重。

    这里只接受 PHASE_STATUS 里的词。**写库写的是归一化后的词**，
    所以项目 3 的「施工中」一旦被改成「进行中」，就统一成标准写法了；
    没动过的格子保持原样，不主动改写用户数据。
    """
    ph = db.get(ProjectPhase, phase_id)
    if ph is None:
        return JSONResponse({"ok": False, "error": "阶段不存在"}, status_code=404)
    word = _norm_phase_status(status)
    if status.strip() not in PHASE_STATUS and PHASE_ALIAS.get(status.strip()) != word:
        return JSONResponse({"ok": False, "error": "未知状态"}, status_code=400)
    ph.status = word
    db.commit()

    # 项目进度是按阶段完成比例算的，改状态必须重算，否则项目列表和
    # 项目详情页的进度条会与这个格子对不上。
    from .projects import recalc_progress
    project = db.get(Project, ph.project_id)
    if project:
        recalc_progress(db, project)

    return JSONResponse({"ok": True, "status": word,
                         "tone": STATE_TONE_MAP.get(word, "idle"),
                         "progress": project.progress if project else None})


# ---------- 任务状态 ----------
# 改版前这里是一整套看板（三列 + 拖拽改列 + 列内排序）。看板已并入
# 阶段看板视图（matrix），拖拽排序随之删掉：三列拖来拖去本质上还是在改
# 一个 status 字段，而矩阵视图点格子就能改，比拖拽少一步。
# 保留下面的 status 接口，给任务行上的下拉用。

@router.post("/gantt/tasks/{task_id}/status")
def gantt_task_status(task_id: int, request: Request,
                      status: str = Form(...), back: str = Form("/gantt"),
                      db: Session = Depends(get_db), user: User = Depends(login_required)):
    """改任务状态（任务行上的下拉）。"""
    word = _norm_phase_status(status)
    if status.strip() not in PHASE_STATUS and PHASE_ALIAS.get(status.strip()) != word:
        raise HTTPException(status_code=400, detail="未知状态")
    task = db.get(GanttTask, task_id)
    if task:
        task.status = word
        db.commit()
    return RedirectResponse(url=_back_url(back), status_code=303)


# ---------- 采购合并页面（采购合同 + 采购单） ----------
@router.get("/procurement")
def procurement_view(request: Request, q: str = "",
                     supplier_id: list[int] = Query([]), project_id: list[int] = Query([]),
                     delivery: list[str] = Query([]), category: list[str] = Query([]),
                     unpaid: list[str] = Query([]),
                     db: Session = Depends(get_db),
                     user: User = Depends(login_required)):
    cq = db.query(PurchaseContract)
    if q:
        like = f"%{q}%"
        cq = cq.filter((PurchaseContract.name.like(like))
                       | (PurchaseContract.contract_no.like(like))
                       | (PurchaseContract.party_a.like(like))
                       | (PurchaseContract.party_b.like(like))
                       | (PurchaseContract.category.like(like))
                       | (PurchaseContract.supplier_id.in_(
                           db.query(Supplier.id).filter(Supplier.name.like(like)))))
    if category:
        cq = cq.filter(PurchaseContract.category.in_(category))
    if supplier_id:
        cq = cq.filter(PurchaseContract.supplier_id.in_(supplier_id))
    if -1 in project_id:
        _others = [x for x in project_id if x != -1]
        if _others:
            cq = cq.filter((PurchaseContract.project_id.is_(None)) | (PurchaseContract.project_id.in_(_others)))
        else:
            cq = cq.filter(PurchaseContract.project_id.is_(None))
    elif project_id:
        cq = cq.filter((PurchaseContract.project_id.in_(project_id)) | (PurchaseContract.project_id.is_(None)))
    if "delivered" in delivery:
        cq = cq.filter(PurchaseContract.delivery_notes.any())
    elif "undelivered" in delivery:
        cq = cq.filter(~PurchaseContract.delivery_notes.any())
    contracts = cq.order_by(PurchaseContract.contract_no.desc(),
                            PurchaseContract.id.desc()).all()

    # 每个合同下：采购单明细 + 财务摘要
    data = []
    for c in contracts:
        orders = db.query(PurchaseOrder).filter(
            PurchaseOrder.contract_id == c.id).order_by(PurchaseOrder.order_date.asc(), PurchaseOrder.id.asc()).all()
        ordered_total = sum(po.total_amount for po in orders)
        progress = round(ordered_total / c.amount * 100, 1) if c.amount else 0
        invoices = db.query(Invoice).filter(Invoice.contract_id == c.id).all()
        invoice_total = sum(i.amount for i in invoices if i.status != "作废")
        pays = db.query(SupplierPayment).filter(SupplierPayment.contract_id == c.id).all()
        paid_total = sum(p.amount for p in pays if p.status != "作废")
        unpaid_amt = max(c.amount - paid_total, 0)
        data.append({"contract": c, "orders": orders,
                     "ordered_total": ordered_total, "progress": progress,
                     "invoice_total": invoice_total,
                     "progress_inv": round(invoice_total / c.amount * 100, 1) if c.amount else 0,
                     "paid_total": paid_total,
                     "progress_paid": round(paid_total / c.amount * 100, 1) if c.amount else 0,
                     "unpaid": unpaid_amt,
                     "progress_unpaid": round(unpaid_amt / c.amount * 100, 1) if c.amount else 0})

    # 按付款状态筛选（有未付 / 已付清），Python 端过滤（data 已含 unpaid 汇总）
    if unpaid and not ("paid" in unpaid and "unpaid" in unpaid):
        if "unpaid" in unpaid and "paid" not in unpaid:
            data = [d for d in data if d["unpaid"] > 0]
        elif "paid" in unpaid:
            data = [d for d in data if d["unpaid"] <= 0]

    return render(request, "procurement.html", {
        "active": "procurement", "user": user, "data": data,
        "q": q, "supplier_id": supplier_id, "project_id": project_id,
        "delivery": delivery, "category": category, "unpaid": unpaid,
        "categories": [r[0] for r in db.query(PurchaseContract.category)
                       .filter(PurchaseContract.category != "").distinct().all()],
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "projects": db.query(Project).order_by(Project.name).all(),
    })
