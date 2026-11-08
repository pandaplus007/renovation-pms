"""费用管理：记录项目实施中除材料采购外的其他费用（人工/机械/设计/管理/运输等）。"""
import io
import urllib.parse
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from openpyxl import Workbook, load_workbook
from sqlalchemy.orm import Session

from ..auth import login_required
from ..config import DATA_DIR
from ..database import get_db
from ..models import COST_CATEGORIES, CostItem, Project, User
from ..utils.files import DOC_EXTS, IMAGE_EXTS, save_upload
from ..utils.audit import log_audit
from ..voided import VOIDED, mark_voided, not_voided, restore
from ..web import render

router = APIRouter(prefix="/expenses", tags=["expenses"])

EXPENSE_EXTS = IMAGE_EXTS | DOC_EXTS


def _date(v: str):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _form_ctx(db: Session, user: User, item=None, error=""):
    return {
        "active": "expenses", "user": user, "item": item, "error": error,
        "projects": db.query(Project).order_by(Project.name).all(),
        # 审批留痕：编辑已有费用时带上单据 id，模板底部渲染审批状态条
        "approval_entity": "cost_item",
        "approval_id": (item.id if item is not None else None),
    }


@router.get("")
def expense_list(request: Request, q: str = "", project_id: list[int] = Query([]), category: list[str] = Query([]),
                 show_voided: int = 0,
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    query = db.query(CostItem)
    if q:
        like = f"%{q}%"
        query = query.filter((CostItem.item_name.like(like)) | (CostItem.note.like(like)))
    if project_id:
        query = query.filter(CostItem.project_id.in_(project_id))
    if category:
        query = query.filter(CostItem.category.in_(category))
    # 作废项默认**藏起来**：它们不进任何金额口径，混在列表里只会让人
    # 对着「合计」逐行加，加出来对不上。要查它们得显式勾选。
    if not show_voided:
        query = query.filter(not_voided(CostItem))
    items = query.order_by(CostItem.occurred_date.desc(), CostItem.id.desc()).all()

    # 合计与分类小计一律排除作废项——即使 show_voided=1 把它们列出来，
    # 页面上的数字也必须是「真实在账上的钱」，否则会被加到总账里去。
    voided_total = sum(i.amount for i in items if i.status == VOIDED)
    live = [i for i in items if i.status != VOIDED]
    total = sum(i.amount for i in live)
    by_category = {}
    for i in live:
        by_category[i.category] = by_category.get(i.category, 0) + i.amount

    return render(request, "expenses/list.html", {
        "active": "expenses", "user": user, "items": items,
        "q": q, "project_id": project_id, "category": category,
        "projects": db.query(Project).order_by(Project.name).all(),
        "total": total,
        "voided_total": voided_total,
        "voided_count": len(items) - len(live),
        "show_voided": show_voided,
        "by_category": sorted(by_category.items(), key=lambda x: -x[1]),
    })


@router.get("/new")
def expense_new(request: Request, db: Session = Depends(get_db),
                user: User = Depends(login_required)):
    return render(request, "expenses/form.html", _form_ctx(db, user))


@router.get("/template")
def expense_template(request: Request, user: User = Depends(login_required)):
    """下载费用导入 Excel 模板。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "费用清单"
    ws.append(["项目名称", "费用类别", "费用项目", "金额", "日期", "备注"])
    ws.append(["领航大厦装饰装修项目（二次）", "人工", "瓦工班组工资", 12800, "2024-01-15", "泥工贴砖"])
    ws.append(["领航大厦装饰装修项目（二次）", "机械", "叉车租赁", 3500, "2024-02-01", "搬运材料"])
    ws.append(["写字楼装修", "管理", "项目管理人员工资", 9000, "2024-02-10", ""])
    for col, width in zip("ABCDEF", [32, 12, 24, 12, 14, 20]):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = urllib.parse.quote("费用导入模板.xlsx")
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.get("/import")
def expense_import_page(request: Request, msg: str = "", error: str = "",
                        result_rows: list = None,
                        db: Session = Depends(get_db), user: User = Depends(login_required)):
    return render(request, "expenses/import.html", {
        "active": "expenses", "user": user, "msg": msg, "error": error,
        "result_rows": result_rows or [],
        "categories": COST_CATEGORIES,
    })


def _exp_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _exp_num(v):
    try:
        if v is None or v == "":
            return 0.0
        return float(str(v).replace(",", "").replace("￥", "").replace("¥", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _parse_expense_file(data: bytes, filename: str) -> list[dict]:
    """解析费用导入 Excel：返回 [{project, category, item_name, amount, date, note}]。"""
    lower = (filename or "").lower()
    if lower.endswith((".xlsx", ".xlsm")):
        wb = load_workbook(io.BytesIO(data), data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    elif lower.endswith(".xls"):
        import xlrd
        wbb = xlrd.open_workbook(file_contents=data)
        ws0 = wbb.sheet_by_index(0)
        rows = [tuple(ws0.row_values(r)) for r in range(ws0.nrows)]
    else:
        raise ValueError("请上传 .xlsx / .xls 格式的 Excel 文件")
    if not rows:
        raise ValueError("Excel 为空")
    header = [str(c or "").strip().lower() for c in rows[0]]

    def col(*names):
        for i, h in enumerate(header):
            if any(n in h for n in names):
                return i
        return -1

    i_proj = col("项目", "project")
    i_cat = col("类别", "分类", "category")
    i_item = col("费用项", "费用项目", "内容", "明细", "item", "name")
    i_amt = col("金额", "amount", "价格", "价钱")
    i_date = col("日期", "date", "时间")
    i_note = col("备注", "note", "说明")
    if i_proj < 0 or i_item < 0 or i_amt < 0:
        raise ValueError("表头需包含：项目名称 / 费用项目 / 金额 列")
    out = []
    for r in rows[1:]:
        def val(idx):
            return r[idx] if 0 <= idx < len(r) else None
        item_name = str(val(i_item) or "").strip()
        if not item_name:
            continue
        out.append({
            "project": str(val(i_proj) or "").strip(),
            "category": (str(val(i_cat) or "").strip()) or "其他费用",
            "item_name": item_name,
            "amount": round(_exp_num(val(i_amt)), 2),
            "date": _exp_date(val(i_date)),
            "note": str(val(i_note) or "").strip(),
        })
    if not out:
        raise ValueError("未解析到有效费用数据，请确认费用项目/金额列有数据")
    return out


@router.post("/import")
async def expense_import_submit(request: Request, file: UploadFile | None = File(None),
                                db: Session = Depends(get_db),
                                user: User = Depends(login_required)):
    """Excel 导入费用：按项目名称匹配项目，逐行创建费用记录。"""
    if not file or not file.filename:
        return RedirectResponse(url="/expenses/import?error=请选择文件", status_code=303)
    try:
        data = await file.read()
        rows = _parse_expense_file(data, file.filename)
    except ValueError as e:
        return RedirectResponse(url=f"/expenses/import?error={urllib.parse.quote(str(e))}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/expenses/import?error={urllib.parse.quote(f'导入失败：{e}')}", status_code=303)

    projects = {p.name: p for p in db.query(Project).all()}
    created = 0
    skipped = []
    result_rows = []
    for r in rows:
        proj = projects.get(r["project"])
        if proj is None and r["project"]:
            # 包含匹配
            for name, p in projects.items():
                if name and (name in r["project"] or r["project"] in name):
                    proj = p
                    break
        if proj is None:
            skipped.append(r["project"] or "（空）")
            result_rows.append({**r, "status": "项目不存在，跳过"})
            continue
        db.add(CostItem(project_id=proj.id, category=r["category"], item_name=r["item_name"],
                        amount=r["amount"], occurred_date=r["date"], note=r["note"]))
        created += 1
        result_rows.append({**r, "status": "已导入"})
    db.commit()
    log_audit(db, user, "导入", "费用", None, f"Excel 导入 {created} 条费用")
    msg = f"导入成功：共 {len(rows)} 行，新增 {created} 条费用"
    if skipped:
        msg += f"；跳过 {len(skipped)} 行（项目不存在：{'、'.join(skipped[:5])}）"
    return render(request, "expenses/import.html", {
        "active": "expenses", "user": user, "msg": msg, "error": "",
        "result_rows": result_rows,
        "categories": COST_CATEGORIES,
    })


@router.post("/new")
async def expense_create(request: Request,
                         project_id: int = Form(...), category: str = Form("其他费用"),
                         item_name: str = Form(...), amount: float = Form(0),
                         occurred_date: str = Form(""), note: str = Form(""),
                         file: UploadFile | None = File(None),
                         db: Session = Depends(get_db), user: User = Depends(login_required)):
    file_path = await save_upload(file, "expense", allowed=EXPENSE_EXTS)
    db.add(CostItem(project_id=project_id, category=category, item_name=item_name,
                    amount=amount, occurred_date=_date(occurred_date), note=note,
                    file_path=file_path or ""))
    db.commit()
    return RedirectResponse(url="/expenses", status_code=303)


@router.get("/{item_id}/edit")
def expense_edit(item_id: int, request: Request,
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    item = db.get(CostItem, item_id)
    if item is None:
        return RedirectResponse(url="/expenses", status_code=303)
    ctx = _form_ctx(db, user, item=item)
    return render(request, "expenses/form.html", ctx)


@router.post("/{item_id}/edit")
async def expense_update(item_id: int, request: Request,
                         project_id: int = Form(...), category: str = Form("其他费用"),
                         item_name: str = Form(...), amount: float = Form(0),
                         occurred_date: str = Form(""), note: str = Form(""),
                         file: UploadFile | None = File(None),
                         db: Session = Depends(get_db), user: User = Depends(login_required)):
    item = db.get(CostItem, item_id)
    if item is None:
        return RedirectResponse(url="/expenses", status_code=303)
    if file and file.filename:
        file_path = await save_upload(file, "expense", allowed=EXPENSE_EXTS)
        if file_path:
            item.file_path = file_path
    item.project_id = project_id
    item.category = category
    item.item_name = item_name
    item.amount = amount
    item.occurred_date = _date(occurred_date)
    item.note = note
    db.commit()
    return RedirectResponse(url="/expenses", status_code=303)


@router.post("/{item_id}/void")
def expense_void(item_id: int, request: Request, reason: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    """作废一笔费用。

    为什么不是删除：费用直接进项目成本与利润。物理删除后账上没有任何痕迹，
    审计日志只能证明「有人删过」，恢复不了。作废后行仍在，可查可导出可恢复，
    但不计入任何金额口径（见 app/voided.py）。
    """
    item = db.get(CostItem, item_id)
    if item is not None and item.status != VOIDED:
        mark_voided(item, reason)
        db.commit()
        log_audit(db, user, "作废", "费用", item_id,
                  f"{item.category or ''}·{item.item_name} ¥{item.amount:,.2f}"
                  + (f"｜原因：{reason.strip()}" if reason and reason.strip() else ""))
    return RedirectResponse(url="/expenses", status_code=303)


@router.post("/{item_id}/restore")
def expense_restore(item_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    """撤销作废（误作废时用）。作废原因留痕不清空——它记录的是「曾经作废过」。"""
    item = db.get(CostItem, item_id)
    if item is not None and item.status == VOIDED:
        restore(item)
        db.commit()
        log_audit(db, user, "恢复", "费用", item_id,
                  f"{item.item_name} ¥{item.amount:,.2f}"
                  + (f"（原作废原因：{item.void_reason}）" if item.void_reason else ""))
    return RedirectResponse(url="/expenses?show_voided=1", status_code=303)
