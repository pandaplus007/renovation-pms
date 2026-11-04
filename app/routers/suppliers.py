"""供应商管理。"""
import re
import urllib.parse

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import (Material, Project, PurchaseOrder, Supplier, SupplierBankAccount,
                      SupplierPayment, User)
from .supplier_payments import _reconciliation
from ..web import render

router = APIRouter(prefix="/suppliers", tags=["suppliers"])


@router.get("")
def supplier_list(request: Request, tab: str = "suppliers", msg: str = "",
                  q: str = "", category: list[str] = Query([]), brand: list[str] = Query([]),
                  coop_level: list[str] = Query([]),
                  supplier_id: list[int] = Query([]), project_id: list[int] = Query([]),
                  db: Session = Depends(get_db),
                  user: User = Depends(login_required)):
    query = db.query(Supplier)
    if q:
        like = f"%{q}%"
        query = query.filter((Supplier.name.like(like)) | (Supplier.contact.like(like))
                             | (Supplier.phone.like(like)) | (Supplier.category.like(like))
                             | (Supplier.brand.like(like)))
    if category:
        query = query.filter(Supplier.category.in_(category))
    if brand:
        query = query.filter(Supplier.brand.in_(brand))
    if coop_level:
        query = query.filter(Supplier.coop_level.in_(coop_level))
    suppliers = query.order_by(Supplier.name).all()
    # 一次性聚合材料数/采购单数，避免 N+1
    _sid = [s.id for s in suppliers]
    _mat_cnt = dict(db.query(Material.supplier_id, func.count(Material.id)).filter(
        Material.supplier_id.in_(_sid)).group_by(Material.supplier_id).all()) if _sid else {}
    _po_cnt = dict(db.query(PurchaseOrder.supplier_id, func.count(PurchaseOrder.id)).filter(
        PurchaseOrder.supplier_id.in_(_sid)).group_by(PurchaseOrder.supplier_id).all()) if _sid else {}
    data = []
    for s in suppliers:
        material_count = _mat_cnt.get(s.id, 0)
        po_count = _po_cnt.get(s.id, 0)
        data.append({"supplier": s, "material_count": material_count, "po_count": po_count})

    # 供应商付款数据（合并到本页 Tab）。
    # 作废项**仍然列出来**（带作废标记）但不计入合计——财务要能看见
    # 「这里有一笔作废记录」才能对账，直接从列表里消失反而会以为没付过。
    reconciliation = _reconciliation(db)
    query = db.query(SupplierPayment)
    if supplier_id:
        query = query.filter(SupplierPayment.supplier_id.in_(supplier_id))
    if project_id:
        query = query.filter(SupplierPayment.project_id.in_(project_id))
    payments = query.order_by(SupplierPayment.pay_date.desc(),
                              SupplierPayment.id.desc()).all()
    total_paid = sum(p.amount for p in payments if p.status != "作废")
    active = "supplier_payments" if tab == "payments" else "suppliers"

    return render(request, "suppliers/list.html", {
        "active": active, "user": user, "data": data, "tab": tab, "msg": msg,
        "q": q, "category": category, "brand": brand, "coop_level": coop_level,
        "categories": [r[0] for r in db.query(Supplier.category)
                       .filter(Supplier.category != "").distinct().order_by(Supplier.category).all()],
        "brands": [r[0] for r in db.query(Supplier.brand)
                   .filter(Supplier.brand != "").distinct().order_by(Supplier.brand).limit(50).all()],
        "coop_levels": [r[0] for r in db.query(Supplier.coop_level)
                        .filter(Supplier.coop_level != "").distinct().order_by(Supplier.coop_level).all()],
        "payments": payments, "reconciliation": reconciliation,
        "total_paid": total_paid,
        "voided_count": sum(1 for p in payments if p.status == "作废"),
        "supplier_id": supplier_id, "project_id": project_id,
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "projects": db.query(Project).order_by(Project.name).all(),
    })


def _parse_supplier_text(text: str) -> dict:
    """解析外部复制的供应商信息（同客户识别）。"""
    r = {"name": "", "tax_no": "", "contact": "", "phone": "", "address": "",
         "bank": "", "account": ""}
    m = re.search(r"[0-9A-HJ-NPQRTUWXY]{18}", text)
    if m:
        r["tax_no"] = m[0]
        text = text.replace(m[0], " ")
    m = re.search(r"(?:单位电话|联系电话|电话|手机)[：:\s]*([0-9\-\s]{7,15})", text)
    if m:
        r["phone"] = m.group(1).strip().replace(" ", "")
    else:
        m = re.search(r"1[3-9]\d{9}|0\d{2,3}-?\d{7,8}", text)
        if m:
            r["phone"] = m[0]
    m = re.search(r"(?:开户银行|开户行|银行名称)[：:\s]*([^\n]{2,60})", text)
    if m:
        r["bank"] = m.group(1).strip()
    m = re.search(r"(?:银行账户|银行账号|账号)[：:\s]*([0-9\s-]{10,30})", text)
    if m:
        r["account"] = m.group(1).replace(" ", "")
    m = re.search(r"[^\n]{0,40}(?:有限公司|有限责任公司|集团有限公司|公司|集团|中心|商行|工厂|建材店|经销部|批发部|代理商)[^\n]{0,40}", text)
    if m:
        r["name"] = re.sub(r"^(?:供应商名称|单位名称|公司名称|名称)\s*[:：]\s*", "", m.group(0).strip())
    m = re.search(r"(?:联系人|负责人|法人|姓名|客户)[：:\s]*([\u4e00-\u9fa5]{2,4})", text)
    if m:
        r["contact"] = m.group(1)
    m = re.search(r"(?:地址|住所|注册地址|公司地址)[：:\s]*([^\n，,。；;]{5,60})", text)
    if m:
        r["address"] = m.group(1).strip()
    return r


@router.post("/parse")
def supplier_parse(request: Request, text: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    """粘贴识别：解析供应商信息，预填新增表单。"""
    parsed = _parse_supplier_text(text or "")
    if not any(parsed.values()):
        request.session["supplier_parsed"] = {"error": "未能识别到供应商信息，请检查格式"}
    else:
        request.session["supplier_parsed"] = parsed
    return RedirectResponse(url="/suppliers/new", status_code=303)


@router.post("/batch-import")
def supplier_batch_import(request: Request, text: str = Form(""),
                          db: Session = Depends(get_db),
                          user: User = Depends(login_required)):
    """批量导入：粘贴多条供应商信息（按“供应商名称/单位名称/名称”标签分段）→ 批量创建。"""
    text = (text or "").strip()
    if not text:
        return RedirectResponse(url="/suppliers/new?msg=请粘贴要导入的供应商信息", status_code=303)
    blocks = re.split(r"(?=(?:供应商名称|单位名称|公司名称|名称)\s*[:：])", text)
    created, skipped = 0, []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        p = _parse_supplier_text(b)
        if not p["name"]:
            skipped.append(b[:20])
            continue
        if db.query(Supplier).filter(Supplier.name == p["name"]).first():
            skipped.append(p["name"])
            continue
        sup = Supplier(name=p["name"], contact=p["contact"], phone=p["phone"],
                       address=p["address"], tax_no=p["tax_no"])
        db.add(sup)
        db.flush()
        if p["bank"] or p["account"]:
            db.add(SupplierBankAccount(supplier_id=sup.id, bank_name=p["bank"],
                                       account_no=p["account"]))
        created += 1
    db.commit()
    msg = f"成功导入 {created} 个供应商"
    if skipped:
        msg += f"；跳过 {len(skipped)} 个（已存在或未识别）：{'、'.join(skipped[:4])}"
    return RedirectResponse(url=f"/suppliers/new?msg={urllib.parse.quote(msg)}", status_code=303)


@router.post("/import")
async def supplier_excel_import(request: Request, file: UploadFile | None = File(None),
                                db: Session = Depends(get_db),
                                user: User = Depends(login_required)):
    """从 Excel 导入供应商（表头：供应商名称/供应品类/品牌/联系人/联系电话/微信号/地址/开户银行/银行账户/供货时长/合作税点/合作方式/合作等级 等）。"""
    if not file or not file.filename:
        return RedirectResponse(url="/suppliers?msg=请选择 Excel 文件", status_code=303)
    import openpyxl
    try:
        wb = openpyxl.load_workbook(file.file, data_only=True)
    except Exception:
        return RedirectResponse(url="/suppliers?msg=Excel 解析失败，请确认文件格式", status_code=303)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return RedirectResponse(url="/suppliers?msg=Excel 为空", status_code=303)
    header = [str(h).strip() if h else "" for h in rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    created = updated = skipped = 0
    for row in rows[1:]:
        def g(key):
            i = idx.get(key)
            return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""
        name = g("供应商名称").strip()
        if not name:
            continue
        sup = db.query(Supplier).filter(Supplier.name == name).first()
        vals = dict(
            category=g("供应品类"), brand=g("品牌"), contact=g("联系人"),
            phone=g("联系电话"), wechat=g("微信号"), address=g("供应商公司地址") or g("地址"),
            profile=g("公司资料"), supply_duration=g("供货时长"),
            tax_point=g("合作税点"), coop_method=g("合作方式"), coop_level=g("合作等级"),
        )
        bank_name = g("开户银行")
        bank_account = g("银行账户")
        if sup:
            for k, v in vals.items():
                if v:
                    setattr(sup, k, v)
            if bank_name or bank_account:
                if not sup.bank_accounts and (bank_name or bank_account):
                    db.add(SupplierBankAccount(supplier_id=sup.id, bank_name=bank_name,
                                               account_no=bank_account))
            updated += 1
        else:
            sup = Supplier(name=name, **vals)
            db.add(sup)
            db.flush()
            if bank_name or bank_account:
                db.add(SupplierBankAccount(supplier_id=sup.id, bank_name=bank_name,
                                           account_no=bank_account))
            created += 1
    db.commit()
    msg = f"导入完成：新增 {created} 个，更新 {updated} 个"
    return RedirectResponse(url=f"/suppliers?msg={urllib.parse.quote(msg)}", status_code=303)


@router.get("/new")
def supplier_new(request: Request, msg: str = "",
                 user: User = Depends(login_required)):
    parsed = request.session.pop("supplier_parsed", None)
    return render(request, "suppliers/form.html", {
        "active": "suppliers", "user": user, "supplier": None,
        "parsed": parsed, "msg": msg,
    })


@router.post("/new")
def supplier_create(request: Request,
                    name: str = Form(...), category: str = Form(""), brand: str = Form(""),
                    contact: str = Form(""), phone: str = Form(""), wechat: str = Form(""),
                    address: str = Form(""), profile: str = Form(""), tax_no: str = Form(""),
                    supply_duration: str = Form(""), tax_point: str = Form(""),
                    coop_method: str = Form(""), coop_level: str = Form(""), note: str = Form(""),
                    bank_names: list[str] = Form([]),
                    bank_accounts: list[str] = Form([]),
                    bank_account_names: list[str] = Form([]),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    supplier = Supplier(name=name, category=category, brand=brand, contact=contact,
                        phone=phone, wechat=wechat, address=address, profile=profile,
                        tax_no=tax_no, supply_duration=supply_duration, tax_point=tax_point,
                        coop_method=coop_method, coop_level=coop_level, note=note)
    db.add(supplier)
    db.flush()
    for i, bn in enumerate(bank_names):
        acct = bank_accounts[i] if i < len(bank_accounts) else ""
        aname = bank_account_names[i] if i < len(bank_account_names) else ""
        if bn.strip() or acct.strip():
            db.add(SupplierBankAccount(supplier_id=supplier.id, bank_name=bn.strip(),
                                       account_no=acct.strip(), account_name=aname.strip()))
    db.commit()
    return RedirectResponse(url="/suppliers", status_code=303)


@router.get("/{supplier_id}/edit")
def supplier_edit(supplier_id: int, request: Request,
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        return RedirectResponse(url="/suppliers", status_code=303)
    return render(request, "suppliers/form.html", {
        "active": "suppliers", "user": user, "supplier": supplier,
    })


@router.post("/{supplier_id}/edit")
def supplier_update(supplier_id: int, request: Request,
                    name: str = Form(...), category: str = Form(""), brand: str = Form(""),
                    contact: str = Form(""), phone: str = Form(""), wechat: str = Form(""),
                    address: str = Form(""), profile: str = Form(""), tax_no: str = Form(""),
                    supply_duration: str = Form(""), tax_point: str = Form(""),
                    coop_method: str = Form(""), coop_level: str = Form(""), note: str = Form(""),
                    bank_names: list[str] = Form([]),
                    bank_accounts: list[str] = Form([]),
                    bank_account_names: list[str] = Form([]),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        return RedirectResponse(url="/suppliers", status_code=303)
    supplier.name = name
    supplier.category = category
    supplier.brand = brand
    supplier.contact = contact
    supplier.phone = phone
    supplier.wechat = wechat
    supplier.address = address
    supplier.profile = profile
    supplier.tax_no = tax_no
    supplier.supply_duration = supply_duration
    supplier.tax_point = tax_point
    supplier.coop_method = coop_method
    supplier.coop_level = coop_level
    supplier.note = note
    # 重建银行账户
    for b in supplier.bank_accounts:
        db.delete(b)
    db.flush()
    for i, bn in enumerate(bank_names):
        acct = bank_accounts[i] if i < len(bank_accounts) else ""
        aname = bank_account_names[i] if i < len(bank_account_names) else ""
        if bn.strip() or acct.strip():
            db.add(SupplierBankAccount(supplier_id=supplier.id, bank_name=bn.strip(),
                                       account_no=acct.strip(), account_name=aname.strip()))
    db.commit()
    return RedirectResponse(url="/suppliers", status_code=303)


@router.post("/{supplier_id}/delete")
def supplier_delete(supplier_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    supplier = db.get(Supplier, supplier_id)
    if supplier:
        db.delete(supplier)
        db.commit()
    return RedirectResponse(url="/suppliers", status_code=303)
