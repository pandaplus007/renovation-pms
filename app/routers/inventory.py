"""库存管理：库存总览、出入库流水。"""
import urllib.parse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import (InventoryRecord, Material, Project, ProjectMaterial,
                      PurchaseOrderItem, User, recalc_stock)
from ..web import render

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.get("")
def inventory_overview(request: Request, q: str = "", msg: str = "",
                       db: Session = Depends(get_db), user: User = Depends(login_required)):
    materials = db.query(Material).order_by(Material.name).all()
    if q:
        like = f"%{q}%"
        materials = [m for m in materials if q in m.name or q in m.spec]

    # 出入库按材料一次性聚合，避免 N+1
    _mids = [m.id for m in materials]
    _agg = {}  # (material_id, record_type) -> sum
    if _mids:
        for _mid, _rtype, _qty in db.query(
                InventoryRecord.material_id, InventoryRecord.record_type,
                func.coalesce(func.sum(InventoryRecord.quantity), 0.0)).filter(
                InventoryRecord.material_id.in_(_mids)).group_by(
                InventoryRecord.material_id, InventoryRecord.record_type).all():
            _agg[(_mid, _rtype)] = float(_qty or 0)
    rows = []
    for m in materials:
        total_in = _agg.get((m.id, "入库"), 0.0)
        total_out = _agg.get((m.id, "出库"), 0.0)
        rows.append({
            "material": m, "total_in": round(total_in, 2),
            "total_out": round(total_out, 2), "stock": round(total_in - total_out, 2),
        })

    projects = db.query(Project).order_by(Project.name).all()
    return render(request, "inventory/list.html", {
        "active": "inventory", "user": user, "rows": rows, "projects": projects,
        "q": q, "msg": msg,
    })


@router.post("/batch-delete")
def inventory_batch_delete(request: Request, ids: list[int] = Form([]),
                           db: Session = Depends(get_db),
                           user: User = Depends(login_required)):
    """批量删除库存材料（仅老板）：级联清理库存记录与项目清单引用，被采购单引用的跳过。"""
    if user.role not in ("老板", "admin"):
        raise HTTPException(status_code=403, detail="仅老板可批量删除库存材料")
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
    return RedirectResponse(url=f"/inventory?msg={urllib.parse.quote(msg)}", status_code=303)


@router.post("/records")
def record_create(request: Request,
                  material_id: int = Form(...), record_type: str = Form("入库"),
                  quantity: float = Form(0), project_id: int = Form(0), note: str = Form(""),
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    if quantity <= 0:
        return RedirectResponse(url="/inventory", status_code=303)
    db.add(InventoryRecord(material_id=material_id, record_type=record_type, quantity=quantity,
                           project_id=project_id or None, note=note))
    _m = db.get(Material, material_id)
    if _m:
        recalc_stock(db, _m)
    db.commit()
    return RedirectResponse(url="/inventory", status_code=303)


@router.get("/records")
def record_list(request: Request, material_id: int = 0,
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    query = db.query(InventoryRecord)
    if material_id:
        query = query.filter(InventoryRecord.material_id == material_id)
    records = query.order_by(InventoryRecord.occurred_at.desc()).limit(200).all()
    materials = db.query(Material).order_by(Material.name).all()
    return render(request, "inventory/records.html", {
        "active": "inventory", "user": user, "records": records,
        "materials": materials, "material_id": material_id,
    })


@router.post("/records/{record_id}/delete")
def record_delete(record_id: int, request: Request,
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    rec = db.get(InventoryRecord, record_id)
    if rec:
        _m = db.get(Material, rec.material_id)
        db.delete(rec)
        if _m:
            recalc_stock(db, _m)
        db.commit()
    return RedirectResponse(url="/inventory/records", status_code=303)
