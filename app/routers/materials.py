"""材料库管理：材料库、Excel 导入、预算 vs 采购对比。"""
import io
import urllib.parse

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from openpyxl import Workbook
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import (
    ContractMaterial, PurchaseContract, InventoryRecord, Material, Project,
                      ProjectMaterial, PurchaseOrderItem, Supplier, User, recalc_stock)
from ..utils.excel_import import parse_material_file
from ..utils.audit import log_audit
from ..web import render

router = APIRouter(prefix="/materials", tags=["materials"])




def _apply_material_price(material: Material, price: float, net_price: float, tax_rate: float) -> None:
    """设置材料含税/不含税单价与税率。
    缺省规则：只有单价时按含税单价处理；只有不含税单价+税率时反算含税；只有含税单价+税率时反算不含税。
    保留原始精度（6 位小数），避免导入后金额与预览不一致。"""
    price = round(price or 0, 6)
    net_price = round(net_price or 0, 6)
    tax_rate = round(tax_rate or 0, 6)
    if net_price and not price:
        # 只有不含税单价 → 含税 = 不含税 * (1 + 税率)
        material.ref_price = round(net_price * (1 + tax_rate / 100), 2)
        material.net_price = net_price
    else:
        if price:
            material.ref_price = price
        if net_price:
            material.net_price = net_price
        elif tax_rate and material.ref_price:
            # 只有含税单价 + 税率 → 反算不含税
            material.net_price = round(material.ref_price / (1 + tax_rate / 100), 2)
    if tax_rate:
        material.tax_rate = tax_rate
    # 保证含税/不含税单价有确定值（无税率时不含税与含税一致或为 0）
    if not material.net_price:
        material.net_price = 0
    if not material.ref_price:
        material.ref_price = 0
    if not material.tax_rate:
        material.tax_rate = 0


def sync_contract_material(db: Session, material: Material, prev_contract_id: int | None = None) -> None:
    """材料设置所属合同后，同步到合同采购清单。
    同一合同内按名称 + 规格 + 单位识别材料，单价变化直接更新原行，避免产生重复清单行；
    若清单行属于另一个不同价格的材料（同合同/名称/规格/单位但价格不同），则新建一行，避免互相覆盖。"""
    if prev_contract_id and prev_contract_id != material.contract_id:
        db.query(ContractMaterial).filter(
            ContractMaterial.contract_id == prev_contract_id,
            ContractMaterial.name == material.name,
            ContractMaterial.spec == (material.spec or ""),
        ).delete(synchronize_session=False)
        db.flush()
    if not material.contract_id:
        return
    _cms = db.query(ContractMaterial).filter(
        ContractMaterial.contract_id == material.contract_id,
        ContractMaterial.name == material.name,
        ContractMaterial.spec == (material.spec or ""),
        ContractMaterial.unit == (material.unit or ""),
    ).order_by(ContractMaterial.id).all()
    _price = material.ref_price or 0

    def _owned_by_other(cm) -> bool:
        """该清单行是否属于另一个不同价格的材料（若是则不覆盖、保留为独立行）。"""
        if not cm.unit_price:
            return False
        others = db.query(Material).filter(
            Material.id != material.id,
            Material.contract_id == material.contract_id,
            Material.name == material.name,
            Material.spec == (material.spec or ""),
            Material.unit == (material.unit or ""),
        ).all()
        return any(abs((o.ref_price or 0) - (cm.unit_price or 0)) < 0.005 for o in others)

    # 优先复用当前单价的行；没有则看是否可覆盖最后一行（仅当该行不属于其它材料）
    cm = next((item for item in _cms if round(item.unit_price or 0, 6) == round(_price, 6)), None)
    if cm is None and _cms and not any(_owned_by_other(item) for item in _cms):
        cm = _cms[-1]
    if cm:
        for duplicate in _cms:
            if duplicate.id != cm.id and not _owned_by_other(duplicate):
                db.delete(duplicate)
        cm.spec = material.spec or cm.spec
        cm.brand = material.brand or cm.brand
        cm.unit = material.unit or cm.unit
        cm.unit_price = _price
        cm.quantity = material.opening_stock or cm.quantity
        cm.amount = round(cm.quantity * cm.unit_price, 2)
    else:
        db.add(ContractMaterial(
            contract_id=material.contract_id, name=material.name,
            spec=material.spec or "", brand=material.brand or "",
            unit=material.unit or "件", quantity=(material.opening_stock or 0),
            unit_price=_price, amount=round((material.opening_stock or 0) * _price, 2)))
    db.flush()

def find_material(db: Session, name: str, spec: str, contract_id: int | None = None,
                  unit: str | None = None, ref_price: float | None = None) -> Material | None:
    """匹配材料库：名称 + 规格 + 所属合同 + 单位一致即合并。
    单价属于可修改属性，不再作为材料身份，避免改价后产生重复材料。"""
    candidates = db.query(Material).filter(func.lower(Material.name) == name.lower()).all()
    if not candidates:
        return None
    _spec = (spec or "").strip()
    _unit = (unit or "").strip()
    for m in candidates:
        if (m.spec or "").strip() != _spec:
            continue
        if contract_id and m.contract_id != contract_id:
            continue
        # 单位不同 → 不合并（同名同型号但单位不同视为不同材料）
        if _unit and (m.unit or "").strip() and (m.unit or "").strip() != _unit:
            continue
        return m
    return None


@router.get("")
def material_list(request: Request, q: str = "", low: str = "", pid: int = 0,
                  msg: str = "", sort: str = "", dir: str = "",
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    query = db.query(Material)
    if q:
        like = f"%{q}%"
        query = query.filter((Material.name.like(like)) | (Material.spec.like(like))
                             | (Material.brand.like(like)))
    # 按项目过滤（侧边栏“按项目查看”树入口）：pid>0 过滤项目及其合同下材料；pid=-1 未分配
    if pid == -1:
        query = query.filter(Material.project_id.is_(None))
    elif pid > 0:
        query = query.filter(or_(
            Material.project_id == pid,
            Material.contract.has(PurchaseContract.project_id == pid),
        ))
    # 排序：支持表头点击排序，默认按所属合同（未关联排最后）再按名称
    _col_map = {
        "name": Material.name, "spec": Material.spec, "brand": Material.brand,
        "unit": Material.unit, "price": Material.ref_price, "stock": Material.stock,
    }
    materials_query = query.outerjoin(Material.contract)
    if sort == "contract":
        _oc = PurchaseContract.name.asc() if dir != "desc" else PurchaseContract.name.desc()
        materials = materials_query.order_by(_oc.nulls_last(), Material.name.asc()).all()
    elif sort in _col_map:
        _col = _col_map[sort]
        _oc = _col.asc() if dir != "desc" else _col.desc()
        materials = materials_query.order_by(_oc, Material.name.asc()).all()
    else:
        # 默认按所属合同排序（未关联合同的排最后），同合同内按名称
        materials = materials_query.order_by(
            PurchaseContract.name.asc().nulls_last(), Material.name.asc()).all()
    if low == "1":
        materials = [m for m in materials if m.low_stock_flag]

    # 预算 vs 采购对比数据（项目材料清单）
    pm_query = db.query(ProjectMaterial)
    if pid:
        pm_query = pm_query.filter(ProjectMaterial.project_id == pid)
    compare_rows = []
    for pm in pm_query.all():
        plan_amount = round(pm.plan_quantity * pm.unit_price, 2)
        purchase_amount = round(pm.purchased_quantity * pm.unit_price, 2)
        compare_rows.append({
            "project_id": pm.project_id,
            "project_name": pm.project.name if pm.project else "—",
            "material": pm.material,
            "plan_quantity": pm.plan_quantity,
            "purchased_quantity": pm.purchased_quantity,
            "unit_price": pm.unit_price,
            "gap": pm.gap,
            "plan_amount": plan_amount,
            "purchase_amount": purchase_amount,
            "rate": (pm.purchased_quantity / pm.plan_quantity * 100) if pm.plan_quantity else 0,
        })
    compare_rows = [x for x in compare_rows if x["material"] is not None]
    compare_rows.sort(key=lambda x: (x["project_name"], x["material"].name))
    projects = db.query(Project).order_by(Project.name).all()
    return render(request, "materials/list.html", {
        "active": "materials", "user": user, "materials": materials, "q": q, "low": low,
        "compare_rows": compare_rows, "projects": projects, "pid": pid, "msg": msg,
        "sort": sort, "dir": dir,
        "contracts": db.query(PurchaseContract).order_by(PurchaseContract.name).all(),
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
    })


@router.get("/new")
def material_new(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    projects = db.query(Project).order_by(Project.name).all()
    return render(request, "materials/form.html", {
        "active": "materials", "user": user, "material": None,
        "suppliers": suppliers, "projects": projects,
        "contracts": db.query(PurchaseContract).order_by(PurchaseContract.name).all(),
    })


@router.post("/new")
def material_create(request: Request,
                    supplier_name: str = Form(""), supplier_id: int = Form(0),
                    project_id: int = Form(0), contract_id: int = Form(0),
                    names: list[str] = Form([]), specs: list[str] = Form([]),
                    model_nos: list[str] = Form([]), brands: list[str] = Form([]),
                    units: list[str] = Form([]),
                    quantities: list[str] = Form([]),
                    ref_prices: list[str] = Form([]),
                    net_prices: list[str] = Form([]),
                    tax_rates: list[str] = Form([]),
                    notes: list[str] = Form([]),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    """新增材料：支持一次填写多行材料（名称/规格/品牌/单位/数量/含税/不含税/税率/备注）。"""
    def _num(v):
        try:
            return float(str(v or "").strip()) if str(v or "").strip() else 0
        except ValueError:
            return 0
    if supplier_name:
        _sn = supplier_name.strip()
        _matched_supplier_id = 0
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                _matched_supplier_id = _s.id
                break
        supplier_id = _matched_supplier_id
    if not supplier_id and contract_id:
        _contract = db.get(PurchaseContract, contract_id)
        if _contract and _contract.supplier_id:
            supplier_id = _contract.supplier_id
    created = 0
    for i, name in enumerate(names):
        name = (name or "").strip()
        if not name:
            continue
        spec = (specs[i] if i < len(specs) else "") or ""
        model_no = (model_nos[i] if i < len(model_nos) else "") or ""
        brand = (brands[i] if i < len(brands) else "") or ""
        unit = (units[i] if i < len(units) else "") or "件"
        qty = _num(quantities[i]) if i < len(quantities) else 0
        ref = _num(ref_prices[i]) if i < len(ref_prices) else 0
        net = _num(net_prices[i]) if i < len(net_prices) else 0
        tax = _num(tax_rates[i]) if i < len(tax_rates) else 0
        note = (notes[i] if i < len(notes) else "") or ""
        material = Material(name=name, spec=spec, model_no=model_no, brand=brand, unit=unit,
                            supplier_id=supplier_id or None, project_id=project_id or None,
                            contract_id=contract_id or None,
                            low_stock=0, opening_stock=qty or 0, note=note)
        _apply_material_price(material, ref, net, tax)
        db.add(material)
        db.flush()
        recalc_stock(db, material)
        sync_contract_material(db, material)
        created += 1
    db.commit()
    log_audit(db, user, "新增", "材料", None, f"批量新增 {created} 个材料")
    return RedirectResponse(url=f"/materials?msg=已新增 {created} 个材料", status_code=303)


# ---------- Excel 导入 ----------
@router.get("/import")
def material_import_page(request: Request, msg: str = "", project_id: int = 0,
                         contract_id: int = 0,
                         db: Session = Depends(get_db), user: User = Depends(login_required)):
    projects = db.query(Project).order_by(Project.name).all()
    _proj = db.get(Project, project_id) if project_id else None
    _ct = db.get(PurchaseContract, contract_id) if contract_id else None
    return render(request, "materials/import.html", {
        "active": "materials", "user": user, "projects": projects, "msg": msg,
        "preselected_project_id": project_id,
        "preselected_project_name": _proj.name if _proj else "",
        "preselected_contract_id": contract_id,
        "preselected_contract_name": _ct.name if _ct else "",
        "contracts": db.query(PurchaseContract).order_by(PurchaseContract.name).all(),
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
    })


@router.post("/preview")
async def material_import_preview(request: Request, file: UploadFile = File(...),
                                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    """预览接口：解析上传的 Excel/Word 材料清单，返回表格行 JSON（供前端预览）。"""
    try:
        data = await file.read()
        rows = parse_material_file(file.filename, data)
        return JSONResponse({"ok": True, "rows": rows})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"预览失败：{e}"})


@router.post("/import")
async def material_import_submit(request: Request,
                                 project_id: int = Form(0), contract_id: int = Form(0),
                                 contract_no: str = Form(""), supplier_id: int = Form(0),
                                 tax_rates: list[str] = Form([]),
                                 net_prices: list[str] = Form([]),
                                 file: UploadFile = File(...),
                                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    projects = db.query(Project).order_by(Project.name).all()
    contracts = db.query(PurchaseContract).order_by(PurchaseContract.name).all()
    # 合同编号自动匹配：编号唯一时解析为合同
    if not contract_id and contract_no.strip():
        _cn = contract_no.strip()
        _matched = [x for x in db.query(PurchaseContract).filter(
            PurchaseContract.contract_no.isnot(None)).all()
            if (x.contract_no or "") == _cn]
        if len(_matched) == 1:
            contract_id = _matched[0].id
    # 未显式选供应商时，自动采用所选合同的供应商
    _contract = db.get(PurchaseContract, contract_id) if contract_id else None
    if not supplier_id and _contract and _contract.supplier_id:
        supplier_id = _contract.supplier_id
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm", ".xls", ".docx", ".doc")):
        return render(request, "materials/import.html", {
            "active": "materials", "user": user, "projects": projects, "contracts": contracts,
            "error": "请上传 .xlsx / .xls / .docx / .doc 格式的文件",
        })
    try:
        data = await file.read()
        rows = parse_material_file(file.filename, data)
    except ValueError as e:
        return render(request, "materials/import.html", {
            "active": "materials", "user": user, "projects": projects, "contracts": contracts,
            "error": str(e),
        })

    project = db.get(Project, project_id) if project_id else None
    new_materials = 0
    added_items = 0
    merged_items = 0
    result_rows = []
    for idx, r in enumerate(rows):
        # 预览中批量填写的税率 / 不含税单价（按行顺序对应）优先于文件解析值
        _tax_rate = 0
        if idx < len(tax_rates) and tax_rates[idx].strip():
            try:
                _tax_rate = float(tax_rates[idx].strip())
            except ValueError:
                _tax_rate = 0
        if not _tax_rate:
            _tax_rate = r.get("tax_rate") or 0
        _net_price = 0
        if idx < len(net_prices) and net_prices[idx].strip():
            try:
                _net_price = float(net_prices[idx].strip())
            except ValueError:
                _net_price = 0
        if not _net_price:
            _net_price = r.get("net_price") or 0
        material = find_material(db, r["name"], r["spec"], contract_id=contract_id or None,
                                  unit=r.get("unit"), ref_price=r.get("price"))
        # 同名但单价不同 → 视为不同价格的材料，不合并（否则合并后金额与预览不符）
        if material and r.get("price") and material.ref_price and abs((material.ref_price or 0) - r["price"]) > 0.005:
            material = None
        status = "已有"
        prev_contract_id = material.contract_id if material else None
        if material is None:
            material = Material(name=r["name"], spec=r["spec"], brand=r.get("brand", ""), unit=r["unit"],
                                contract_id=contract_id or None,
                                supplier_id=supplier_id or None,
                                project_id=project.id if project else None)
            db.add(material)
            db.flush()
            new_materials += 1
            status = "新增"
        _apply_material_price(material, r.get("price") or 0,
                              _net_price, _tax_rate)
        if r.get("note"):
            material.note = (material.note or "") + (("；" + r["note"]) if material.note else r["note"])
        if r.get("brand") and not material.brand:
            material.brand = r["brand"]
        if contract_id and not material.contract_id:
            material.contract_id = contract_id
        if supplier_id and not material.supplier_id:
            material.supplier_id = supplier_id
        if project and not material.project_id:
            material.project_id = project.id
        # 导入数量计入材料期初库存（列表「数量」列可见）
        material.opening_stock = (material.opening_stock or 0) + r["quantity"]
        recalc_stock(db, material)
        sync_contract_material(db, material, prev_contract_id=prev_contract_id)
        if project:
            pm = db.query(ProjectMaterial).filter_by(
                project_id=project.id, material_id=material.id).first()
            if pm:
                pm.plan_quantity += r["quantity"]
                if r["price"]:
                    pm.unit_price = r["price"]
                merged_items += 1
            else:
                db.add(ProjectMaterial(project_id=project.id, material_id=material.id,
                                       plan_quantity=r["quantity"], purchased_quantity=0,
                                       unit_price=r["price"]))
                added_items += 1
        result_rows.append({
            "name": r["name"], "spec": r["spec"], "brand": r.get("brand", ""), "unit": r["unit"],
            "quantity": r["quantity"], "price": r["price"],
            "net_price": _net_price, "tax_rate": _tax_rate,
            "note": r.get("note") or "",
            "status": status,
        })
    db.commit()

    msg = f"导入成功：共 {len(rows)} 行材料，新增材料 {new_materials} 种"
    if project:
        msg += f"，已写入「{project.name}」预算清单（新增 {added_items} 项、合并 {merged_items} 项）"
    else:
        msg += "（未选择项目，仅扩充材料库）"
    return render(request, "materials/import.html", {
        "active": "materials", "user": user, "projects": projects, "contracts": contracts,
        "msg": msg, "result_rows": result_rows, "project": project,
        "preselected_project_id": project_id, "preselected_contract_id": contract_id,
    })


@router.get("/template")
def material_template(request: Request, user: User = Depends(login_required)):
    """下载 Excel 导入模板。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "材料清单"
    ws.append(["材料名称", "规格型号", "单位", "数量", "单价（含税）", "不含税单价", "税率%", "备注"])
    ws.append(["瓷砖", "800×800 通体大理石", "片", 120, 68, 60.18, 13, "优等品"])
    ws.append(["水泥", "P.O 42.5 / 50kg", "袋", 60, 25, 22.94, 9, ""])
    ws.append(["乳胶漆", "18L 净味", "桶", 6, 380, 380, 0, ""])
    for col, width in zip("ABCDEFG", [20, 26, 8, 10, 12, 12, 8]):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    # 中文文件名需使用 RFC 5987 编码
    filename = urllib.parse.quote("材料清单模板.xlsx")
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.get("/{material_id}")
def material_detail(material_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    material = db.get(Material, material_id)
    if material is None:
        return RedirectResponse(url="/materials", status_code=303)
    return render(request, "materials/detail.html", {
        "active": "materials", "user": user, "material": material,
    })


@router.get("/{material_id}/edit")
def material_edit(material_id: int, request: Request,
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    material = db.get(Material, material_id)
    if material is None:
        return RedirectResponse(url="/materials", status_code=303)
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    projects = db.query(Project).order_by(Project.name).all()
    return render(request, "materials/form.html", {
        "active": "materials", "user": user, "material": material,
        "suppliers": suppliers, "projects": projects,
        "contracts": db.query(PurchaseContract).order_by(PurchaseContract.name).all(),
    })


@router.post("/{material_id}/edit")
def material_update(material_id: int, request: Request,
                    name: str = Form(...), spec: str = Form(""), model_no: str = Form(""), brand: str = Form(""),
                    unit: str = Form("件"), ref_price: float = Form(0), supplier_name: str = Form(""),
                    supplier_id: int = Form(0),
                    project_id: int = Form(0), contract_id: int = Form(0),
                    net_price: float = Form(0), tax_rate: float = Form(0),
                    low_stock: float = Form(0), opening_stock: float = Form(0), note: str = Form(""),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    material = db.get(Material, material_id)
    if material is None:
        return RedirectResponse(url="/materials", status_code=303)
    prev_contract_id = material.contract_id
    material.name = name
    material.spec = spec
    material.model_no = model_no
    material.brand = brand
    material.unit = unit
    _apply_material_price(material, ref_price, net_price, tax_rate)
    if supplier_name:
        _sn = supplier_name.strip()
        _matched_supplier_id = 0
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                _matched_supplier_id = _s.id
                break
        supplier_id = _matched_supplier_id
    if not supplier_id and contract_id:
        _contract = db.get(PurchaseContract, contract_id)
        if _contract and _contract.supplier_id:
            supplier_id = _contract.supplier_id
    material.supplier_id = supplier_id or None
    material.project_id = project_id or None
    material.contract_id = contract_id or None
    material.low_stock = low_stock
    material.opening_stock = opening_stock
    material.note = note
    recalc_stock(db, material)
    sync_contract_material(db, material, prev_contract_id=prev_contract_id)
    db.commit()
    log_audit(db, user, "修改", "材料", material_id, f"{material.name} {material.spec or ''}".strip())
    return RedirectResponse(url=f"/materials?msg=已保存材料「{material.name}」", status_code=303)


@router.post("/batch-update")
def material_batch_update(request: Request, ids: list[int] = Form([]),
                          supplier_name: str = Form(""), supplier_id: int = Form(0),
                          project_id: int = Form(0), contract_id: int = Form(0),
                          ref_price: float = Form(-1), low_stock: float = Form(-1),
                          opening_stock: float = Form(-1),
                          unit: str = Form(""), brand: str = Form(""),
                          db: Session = Depends(get_db), user: User = Depends(login_required)):
    """批量修改材料属性：留空/0 表示不修改（单价/预警用 -1 区分未设置）。"""
    if not ids:
        return RedirectResponse(url="/materials?msg=请先勾选材料", status_code=303)
    if not supplier_id and supplier_name:
        _sn = supplier_name.strip()
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                supplier_id = _s.id
                break
    n = 0
    for mid in ids:
        mat = db.get(Material, mid)
        if mat is None:
            continue
        prev_cid = mat.contract_id
        if supplier_id:
            mat.supplier_id = supplier_id
        if project_id:
            mat.project_id = project_id
        if contract_id:
            mat.contract_id = contract_id
        if ref_price >= 0:
            mat.ref_price = ref_price
        if low_stock >= 0:
            mat.low_stock = low_stock
        if opening_stock >= 0:
            mat.opening_stock = opening_stock
        if unit.strip():
            mat.unit = unit.strip()
        if brand.strip():
            mat.brand = brand.strip()
        recalc_stock(db, mat)
        sync_contract_material(db, mat, prev_contract_id=prev_cid)
        n += 1
    db.commit()
    return RedirectResponse(url=f"/materials?msg=已批量修改 {n} 个材料", status_code=303)


@router.post("/batch-delete")
def material_batch_delete(request: Request, ids: list[int] = Form([]),
                          db: Session = Depends(get_db),
                          user: User = Depends(login_required)):
    """批量删除材料：级联清理库存记录与项目清单引用；被采购单引用的材料跳过。"""
    ok, skipped = 0, []
    for mid in ids:
        material = db.get(Material, mid)
        if material is None:
            continue
        po_used = db.query(PurchaseOrderItem).filter(
            PurchaseOrderItem.material_id == mid).count()
        if po_used:
            skipped.append(material.name)
            continue
        db.query(ProjectMaterial).filter(ProjectMaterial.material_id == mid).delete()
        db.query(InventoryRecord).filter(InventoryRecord.material_id == mid).delete()
        db.delete(material)
        ok += 1
    db.commit()
    msg = f"已删除 {ok} 个材料"
    if skipped:
        msg += f"；跳过 {len(skipped)} 个（被采购单引用）：{'、'.join(skipped[:6])}"
    log_audit(db, user, "批量删除", "材料", None, msg)
    return RedirectResponse(url=f"/materials?msg={urllib.parse.quote(msg)}", status_code=303)


@router.post("/{material_id}/delete")
def material_delete(material_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    material = db.get(Material, material_id)
    if material is None:
        return RedirectResponse(url="/materials", status_code=303)
    po_used = db.query(PurchaseOrderItem).filter(
        PurchaseOrderItem.material_id == material_id).count()
    if po_used:
        return render(request, "materials/detail.html", {
            "active": "materials", "user": user, "material": material,
            "error": f"该材料已被采购单明细引用({po_used}处)，无法删除；请先移除采购单中的相关材料。",
        })
    # 级联清理：项目材料清单引用 + 库存记录
    db.query(ProjectMaterial).filter(ProjectMaterial.material_id == material_id).delete()
    db.query(InventoryRecord).filter(InventoryRecord.material_id == material_id).delete()
    _mname = f"{material.name} {material.spec or ''}".strip()
    db.delete(material)
    db.commit()
    log_audit(db, user, "删除", "材料", material_id, _mname)
    return RedirectResponse(url="/materials", status_code=303)
