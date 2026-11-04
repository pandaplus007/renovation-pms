"""公司资料知识库：合同模板 / 询价模板等资料管理。"""
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import COMP_DOC_CATEGORIES, CompanyDoc, User
from ..utils.files import DOC_EXTS, save_upload
from ..web import render

router = APIRouter(prefix="/company-docs", tags=["company_docs"])


@router.get("")
def doc_list(request: Request, q: str = "", category: str = "",
             db: Session = Depends(get_db), user: User = Depends(login_required)):
    query = db.query(CompanyDoc)
    if q:
        like = f"%{q}%"
        query = query.filter((CompanyDoc.title.like(like)) | (CompanyDoc.note.like(like)))
    if category:
        query = query.filter(CompanyDoc.category == category)
    docs = query.order_by(CompanyDoc.created_at.desc()).all()
    return render(request, "company_docs/list.html", {
        "active": "company_docs", "user": user, "docs": docs,
        "categories": COMP_DOC_CATEGORIES, "q": q, "category": category,
    })


@router.post("")
async def doc_create(request: Request,
                     title: str = Form(""), category: str = Form("其他"),
                     note: str = Form(""), file: UploadFile | None = File(None),
                     db: Session = Depends(get_db), user: User = Depends(login_required)):
    file_path = await save_upload(file, "company_docs", allowed=DOC_EXTS)
    if not file_path:
        return RedirectResponse(url="/company-docs?msg=请选择要上传的文件", status_code=303)
    title = title.strip() or file.filename or "未命名资料"
    db.add(CompanyDoc(title=title, category=category or "其他", note=note,
                      file_path=file_path))
    db.commit()
    return RedirectResponse(url="/company-docs", status_code=303)


@router.post("/{doc_id}/delete")
def doc_delete(doc_id: int, request: Request,
               db: Session = Depends(get_db), user: User = Depends(login_required)):
    doc = db.get(CompanyDoc, doc_id)
    if doc:
        db.delete(doc)
        db.commit()
    return RedirectResponse(url="/company-docs", status_code=303)
