"""施工阶段模板管理：自定义新增/删除全局施工阶段（新项目自动复制）。"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import Phase, ProjectPhase, User
from ..web import render

router = APIRouter(prefix="/phases", tags=["phases"])


@router.get("")
def phase_list(request: Request, msg: str = "",
               db: Session = Depends(get_db), user: User = Depends(login_required)):
    phases = db.query(Phase).order_by(Phase.sort_order).all()
    data = []
    for ph in phases:
        used = db.query(ProjectPhase).filter(ProjectPhase.name == ph.name).count()
        data.append({"phase": ph, "used": used})
    return render(request, "phases/list.html", {
        "active": "phases", "user": user, "data": data, "msg": msg,
    })


@router.post("")
def phase_create(request: Request, name: str = Form(...),
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    name = name.strip()
    if name:
        exist = db.query(Phase).filter(Phase.name == name).first()
        if exist:
            return RedirectResponse(url="/phases?msg=该阶段已存在", status_code=303)
        max_order = db.query(func.max(Phase.sort_order)).scalar() or 0
        db.add(Phase(name=name, sort_order=max_order + 1))
        db.commit()
    return RedirectResponse(url="/phases", status_code=303)


@router.post("/{phase_id}/delete")
def phase_delete(phase_id: int, request: Request,
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    ph = db.get(Phase, phase_id)
    if ph:
        used = db.query(ProjectPhase).filter(ProjectPhase.name == ph.name).count()
        if used:
            return RedirectResponse(
                url=f"/phases?msg=该阶段已被 {used} 个项目使用，无法删除；可先在项目详情移除对应阶段。",
                status_code=303)
        db.delete(ph)
        db.commit()
    return RedirectResponse(url="/phases", status_code=303)
