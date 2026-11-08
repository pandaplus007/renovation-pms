"""供应商付款管理：付款记录、银行转账单导入识别与存档、供应商对账。"""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import (Project, PurchaseContract, PurchaseOrder, PurchaseOrderItem,
                      SubContract, SubOrder, Supplier, SupplierPayment, User)
from ..utils.bank_parser import match_supplier, recognize_bank_file
from ..utils.excel_export import export_xlsx
from ..utils.files import IMAGE_EXTS, save_upload
from ..utils.audit import log_audit
from ..voided import VOIDED, mark_voided, not_voided, restore
from ..web import render

router = APIRouter(prefix="/supplier-payments", tags=["supplier-payments"])

BANK_EXTS = IMAGE_EXTS | {".pdf"}


def _date(v: str):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _form_ctx(db: Session, user: User, payment=None, parsed=None, bank_file="",
              match=None, error=""):
    return {
        "active": "supplier_payments", "user": user, "payment": payment,
        "parsed": parsed, "bank_file": bank_file, "match": match, "error": error,
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "projects": db.query(Project).order_by(Project.name).all(),
        "pos": db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).all(),
        "contracts": db.query(PurchaseContract).order_by(PurchaseContract.contract_no.desc()).all(),
        # 审批留痕：编辑已有付款时带上单据 id，模板底部渲染审批状态条
        "approval_entity": "supplier_payment",
        "approval_id": (payment.id if payment is not None else None),
    }


def _reconciliation(db: Session):
    """供应商对账：应付 = 采购单合计 + 分包子单合计（乙方按名称匹配供应商），已付 = 付款合计。"""
    # 应付①：采购单明细金额按供应商聚合
    payable_rows = db.query(
        PurchaseOrder.supplier_id,
        func.coalesce(func.sum(PurchaseOrderItem.quantity * PurchaseOrderItem.unit_price), 0.0),
    ).join(PurchaseOrderItem, PurchaseOrderItem.po_id == PurchaseOrder.id) \
     .filter(PurchaseOrder.supplier_id.isnot(None)) \
     .group_by(PurchaseOrder.supplier_id).all()
    payable_map = {sid: float(amt or 0) for sid, amt in payable_rows}
    # 应付②：分包合同按乙方名称匹配供应商（有子单用子单合计，无子单用合同额）
    sub_rows = db.query(
        SubContract.party_b,
        SubContract.amount,
        func.coalesce(func.sum(SubOrder.total_amount), 0.0),
    ).outerjoin(SubOrder, SubOrder.subcontract_id == SubContract.id) \
     .filter(SubContract.party_b != "") \
     .group_by(SubContract.party_b, SubContract.amount).all()
    sup_all = db.query(Supplier).all()
    for party_b, amount, sub_sum in sub_rows:
        sid = None
        for s in sup_all:  # 精确匹配
            if s.name == party_b:
                sid = s.id; break
        if sid is None:    # 包含匹配（乙方名包含供应商名，或反之）
            for s in sup_all:
                if s.name and (s.name in party_b or party_b in s.name):
                    sid = s.id; break
        if sid is None:
            continue
        v = sub_sum if sub_sum > 0 else (amount or 0)
        payable_map[sid] = payable_map.get(sid, 0.0) + v
    # 已付：付款按供应商聚合。作废的付款是「录错了不算数」，
    # 拿它算余额会让对账单凭空多出一笔欠款，供应商一看就说这单我们早付过了。
    paid_rows = db.query(
        SupplierPayment.supplier_id,
        func.coalesce(func.sum(SupplierPayment.amount), 0.0),
    ).filter(not_voided(SupplierPayment)).group_by(SupplierPayment.supplier_id).all()
    paid_map = {sid: float(amt or 0) for sid, amt in paid_rows}
    result = []
    for s in db.query(Supplier).order_by(Supplier.name).all():
        payable = payable_map.get(s.id, 0.0)
        paid = paid_map.get(s.id, 0.0)
        result.append({
            "supplier": s, "payable": round(payable, 2),
            "paid": round(paid, 2), "balance": round(payable - paid, 2),
        })
    result.sort(key=lambda x: -x["balance"])
    return result


@router.get("")
def payments_legacy_redirect(request: Request,
                             db: Session = Depends(get_db),
                             user: User = Depends(login_required)):
    """旧链接 /supplier-payments → 合并页付款 Tab（兼容旧书签/刷新）。"""
    return RedirectResponse(url="/suppliers?tab=payments", status_code=307)


@router.get("/export")
def payment_export(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(login_required)):
    reconciliation = _reconciliation(db)
    data = []
    for r in reconciliation:
        status = ("已结清" if r["payable"] > 0 and r["balance"] <= 0
                  else "部分结算" if r["paid"] > 0 else "未付款")
        data.append([r["supplier"].name, r["supplier"].contact,
                     r["payable"], r["paid"], r["balance"], status])
    return export_xlsx(
        "供应商对账.xlsx",
        ["供应商", "联系人", "应付(采购单)", "已付", "欠款", "结算状态"],
        data, "供应商对账",
    )


@router.get("/new")
def payment_new(request: Request, contract_id: int = 0, po_id: int = 0,
                subcontract_id: int = 0, back_url: str = "",
                db: Session = Depends(get_db),
                user: User = Depends(login_required)):
    ctx = _form_ctx(db, user)
    ctx["back_url"] = back_url
    ctx["prefill_contract_id"] = contract_id
    ctx["prefill_po_id"] = po_id
    ctx["prefill_subcontract_id"] = subcontract_id
    ctx["suborders"] = (db.query(SubOrder).filter(SubOrder.subcontract_id == subcontract_id).order_by(SubOrder.id.desc()).all()
                        if subcontract_id else [])
    ctx["subcontracts"] = db.query(SubContract).order_by(SubContract.contract_no.desc(), SubContract.id.desc()).all()
    if subcontract_id:
        _sc = db.get(SubContract, subcontract_id)
        if _sc:
            if not ctx.get("prefill_project_id") and _sc.project_id:
                ctx["prefill_project_id"] = _sc.project_id
            # 供应商自动填入分包合同乙方
            if _sc.party_b and not ctx.get("prefill_supplier_id"):
                ctx["prefill_supplier_name"] = _sc.party_b
                for _s in db.query(Supplier).all():
                    if _s.name == _sc.party_b or (_sc.party_b in (_s.name or "") or (_s.name or "") in _sc.party_b):
                        ctx["prefill_supplier_id"] = _s.id
                        break
    # 从采购合同详情登记付款：自动带出供应商与关联项目；采购单限定为该合同范围
    ctx["prefill_supplier_id"] = 0
    if not ctx.get("prefill_project_id"):
        ctx["prefill_project_id"] = 0
    if contract_id:
        _pc = db.get(PurchaseContract, contract_id)
        if _pc:
            ctx["prefill_supplier_id"] = _pc.supplier_id or 0
            ctx["prefill_project_id"] = _pc.project_id or 0
        ctx["pos"] = db.query(PurchaseOrder).filter(
            PurchaseOrder.contract_id == contract_id).order_by(PurchaseOrder.id.desc()).all()
    return render(request, "supplier_payments/form.html", ctx)


@router.post("/parse")
async def payment_parse(request: Request, file: UploadFile | None = File(None),
                        supplier_id: int = Form(0), project_id: int = Form(0),
                        contract_id: int = Form(0), po_id: int = Form(0),
                        subcontract_id: int = Form(0), suborder_id: int = Form(0),
                        back_url: str = Form(""),
                        db: Session = Depends(get_db), user: User = Depends(login_required)):
    """上传银行转账单 → 识别（PDF 规则 / 图片视觉）→ 预填表单并存档。"""
    ctx = _form_ctx(db, user)
    ctx["back_url"] = back_url
    ctx["prefill_supplier_id"] = supplier_id
    ctx["prefill_project_id"] = project_id
    ctx["prefill_contract_id"] = contract_id
    ctx["prefill_po_id"] = po_id
    ctx["prefill_subcontract_id"] = subcontract_id
    ctx["prefill_suborder_id"] = suborder_id
    ctx["subcontracts"] = db.query(SubContract).order_by(SubContract.contract_no.desc(), SubContract.id.desc()).all()
    ctx["suborders"] = (db.query(SubOrder).filter(SubOrder.subcontract_id == subcontract_id).order_by(SubOrder.id.desc()).all()
                        if subcontract_id else [])
    # 从分包合同登记付款：识别后保留乙方供应商与项目
    if subcontract_id:
        _sc = db.get(SubContract, subcontract_id)
        if _sc:
            if not ctx.get("prefill_project_id") and _sc.project_id:
                ctx["prefill_project_id"] = _sc.project_id
            if _sc.party_b and not ctx.get("prefill_supplier_id"):
                ctx["prefill_supplier_name"] = _sc.party_b
                for _s in db.query(Supplier).all():
                    if _s.name == _sc.party_b or (_sc.party_b in (_s.name or "") or (_s.name or "") in _sc.party_b):
                        ctx["prefill_supplier_id"] = _s.id
                        break
    # 从采购合同登记付款：识别后保留合同的供应商/项目，采购单限定为该合同范围
    if contract_id:
        ctx["pos"] = db.query(PurchaseOrder).filter(
            PurchaseOrder.contract_id == contract_id).order_by(PurchaseOrder.id.desc()).all()
        _pc = db.get(PurchaseContract, contract_id)
        if _pc:
            if not ctx.get("prefill_supplier_id") and _pc.supplier_id:
                ctx["prefill_supplier_id"] = _pc.supplier_id
            if not ctx.get("prefill_project_id") and _pc.project_id:
                ctx["prefill_project_id"] = _pc.project_id
            # 供应商名称（乙方）
            if _pc.party_b and not ctx.get("prefill_supplier_name"):
                ctx["prefill_supplier_name"] = _pc.party_b
    if file is None or not (file.filename or "").strip():
        ctx["error"] = "请选择转账单文件"
        return render(request, "supplier_payments/form.html", ctx)

    data = await file.read()
    file.file.seek(0)
    bank_file = await save_upload(file, "bank", allowed=BANK_EXTS)
    if not bank_file:
        ctx["error"] = "文件保存失败（支持 png / jpg / pdf，不超过 20MB）"
        return render(request, "supplier_payments/form.html", ctx)
    ctx["bank_file"] = bank_file

    parsed, parse_err = recognize_bank_file(file.filename, data)
    if parsed and (parsed.get("amount") or parsed.get("payee")):
        ctx["parsed"] = parsed
        _payee = parsed.get("payee", "")
        supplier = match_supplier(db, _payee)
        _fuzzy = False
        if supplier and _payee and supplier.name:
            _fuzzy = (_payee != supplier.name and _payee not in supplier.name
                      and supplier.name not in _payee)
        ctx["match"] = {"supplier": supplier, "payee": _payee, "fuzzy": _fuzzy}
    else:
        ctx["error"] = (parse_err or "未能自动识别该文件内容（可能是扫描件或格式特殊）"
                        ).rstrip("。") + "；文件已存档。"
    return render(request, "supplier_payments/form.html", ctx)


@router.post("/new")
def payment_create(request: Request,
                   supplier_name: str = Form(""),
                   supplier_id: int = Form(...), amount: float = Form(0),
                   pay_date: str = Form(""), pay_method: str = Form("银行转账"),
                   project_id: int = Form(0), po_id: int = Form(0), contract_id: int = Form(0),
                   note: str = Form(""), back_url: str = Form(""),
                   subcontract_id: int = Form(0), suborder_id: int = Form(0),
                   bank_file: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not supplier_id and supplier_name:
        _sn = supplier_name.strip()
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                supplier_id = _s.id
                break
    # 用对象本身拿 id，**不要**用「查 id 最大的一条」——
    # 两个人同时登记付款时，A flush 之后 B 也 flush，A 再查 desc 就可能拿到
    # B 的 id，审计日志记到别人头上，且不报任何错。
    obj = SupplierPayment(supplier_id=supplier_id, amount=amount, pay_date=_date(pay_date),
                           pay_method=pay_method, bank_file=bank_file,
                           project_id=project_id or None, contract_id=contract_id or None,
                           subcontract_id=subcontract_id or None, suborder_id=suborder_id or None,
                           po_id=po_id or None, note=note)
    db.add(obj)
    db.commit()
    log_audit(db, user, "新增", "付款", obj.id, f"¥{amount} {pay_method}")
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url="/supplier-payments", status_code=303)


@router.get("/{payment_id}/edit")
def payment_edit(payment_id: int, request: Request, back_url: str = "",
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    payment = db.get(SupplierPayment, payment_id)
    if payment is None:
        return RedirectResponse(url="/supplier-payments", status_code=303)
    ctx = _form_ctx(db, user, payment=payment, bank_file=payment.bank_file)
    ctx["back_url"] = back_url
    ctx["prefill_contract_id"] = payment.contract_id or 0
    ctx["prefill_po_id"] = payment.po_id or 0
    ctx["prefill_subcontract_id"] = payment.subcontract_id or 0
    ctx["prefill_project_id"] = payment.project_id or 0
    ctx["suborders"] = db.query(SubOrder).order_by(SubOrder.id.desc()).all()
    ctx["subcontracts"] = db.query(SubContract).order_by(SubContract.contract_no.desc(), SubContract.id.desc()).all()
    return render(request, "supplier_payments/form.html", ctx)


@router.post("/{payment_id}/edit")
def payment_update(payment_id: int, request: Request,
                   supplier_name: str = Form(""),
                   supplier_id: int = Form(...), amount: float = Form(0),
                   pay_date: str = Form(""), pay_method: str = Form("银行转账"),
                   project_id: int = Form(0), po_id: int = Form(0), contract_id: int = Form(0),
                   subcontract_id: int = Form(0), suborder_id: int = Form(0),
                   note: str = Form(""), back_url: str = Form(""),
                   bank_file: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    payment = db.get(SupplierPayment, payment_id)
    if payment is None:
        return RedirectResponse(url="/supplier-payments", status_code=303)
    if not supplier_id and supplier_name:
        _sn = supplier_name.strip()
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                supplier_id = _s.id
                break
    payment.supplier_id = supplier_id
    payment.amount = amount
    payment.pay_date = _date(pay_date)
    payment.pay_method = pay_method
    payment.bank_file = bank_file
    payment.contract_id = contract_id or None
    payment.project_id = project_id or None
    payment.po_id = po_id or None
    payment.subcontract_id = subcontract_id or None
    payment.suborder_id = suborder_id or None
    payment.note = note
    db.commit()
    log_audit(db, user, "修改", "付款", payment_id, f"¥{amount} {pay_method}")
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url="/supplier-payments", status_code=303)


@router.post("/{payment_id}/void")
def payment_void(payment_id: int, request: Request, reason: str = Form(""),
                 back_url: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    """作废一笔付款（不物理删除）。

    付款是超付预警、应付账款、供应商对账、毛利的共同输入。删掉之后这些
    数字会跳变，且没有任何痕迹说明发生了什么。作废后行保留、不入账。
    """
    payment = db.get(SupplierPayment, payment_id)
    if payment is not None and payment.status != VOIDED:
        mark_voided(payment, reason)
        db.commit()
        log_audit(db, user, "作废", "付款", payment_id,
                  f"¥{payment.amount:,.2f} {payment.pay_method or ''}".strip()
                  + (f"｜原因：{reason.strip()}" if reason and reason.strip() else ""))
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url="/suppliers?tab=payments", status_code=303)


@router.post("/{payment_id}/restore")
def payment_restore(payment_id: int, request: Request, back_url: str = Form(""),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    """撤销作废（误作废时用）。"""
    payment = db.get(SupplierPayment, payment_id)
    if payment is not None and payment.status == VOIDED:
        restore(payment)
        db.commit()
        log_audit(db, user, "恢复", "付款", payment_id,
                  f"¥{payment.amount:,.2f}"
                  + (f"（原作废原因：{payment.void_reason}）" if payment.void_reason else ""))
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url="/suppliers?tab=payments", status_code=303)
