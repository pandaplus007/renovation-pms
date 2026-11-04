"""报价单（签单漏斗）路由。

回答三个问题：
  1. 报了多少单、成了多少 → 签单率（金额与笔数两种口径）
  2. 没成的为什么没成 → 流失原因分布
  3. 我报的价合不合理 → 按需求类型的综合单价基准
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import (QUOTE_CATEGORIES, QUOTE_STATUS, Customer, Project,
                      Quotation, QuotationItem, User)
from ..quotes import (FUNNEL_COUNTED, by_customer, funnel, list_quotes,
                      status_counts, unit_price_benchmark)
from ..utils.audit import log_audit
from ..utils.excel_export import export_xlsx
from ..web import render

router = APIRouter(prefix="/quotes", tags=["quotes"])


def _f(v: str | None, d=0.0) -> float:
    try:
        return float(v) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _d(v: str | None):
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return None


# ================================================================== 漏斗主页

@router.get("")
def quote_index(request: Request, status: str = "", q: str = "",
                customer_id: int = 0,
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    f = funnel(db)
    rows = list_quotes(db, customer_id or None, status, q)
    counts = status_counts(db)
    return render(request, "quotes/index.html", {
        "active": "quotes", "user": user,
        "f": f, "rows": rows, "counts": counts,
        "status": status, "q": q, "customer_id": customer_id,
        "statuses": QUOTE_STATUS,
        "by_cust": by_customer(db),
        "bench": unit_price_benchmark(db),
        "customers": db.query(Customer).order_by(Customer.name).all(),
        "projects": db.query(Project).order_by(Project.name).all(),
    })


# ---------------------------------------------------------------- 静态路由必须在前
# 下面三条要放在 /{qid} 之前。FastAPI 按注册顺序匹配，先命中 /{qid} 的话
# 「/export」「/new」会被当成 qid 去查数据库，找不到就 303 回列表——
# 表现是「点了新增报价单，页面悄悄跳回列表」，没有报错，最难查。
@router.get("/export")
def quote_export(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    rows = list_quotes(db)
    data = []
    for r in rows:
        data.append([
            r["customer_name"], r["no"], r["title"], r["community"], r["area"],
            r["requirement_type"], r["unit_price"], r["discount"], r["total"],
            r["status"], r["quote_date"] or "", r["valid_until"] or "",
            r["item_count"], r["lost_reason"],
        ])
    f = funnel(db)
    data.append([])
    data.append([f"签单率：按金额 {f['win_rate_amt']}%　按笔数 {f['win_rate_cnt']}%"])
    data.append([f"报出 {f['quoted_count']} 笔 / {f['quoted_amount']:,.2f}　"
                 f"成交 {f['won_count']} 笔 / {f['won_amount']:,.2f}　"
                 f"流失 {f['lost_count']} 笔"])
    return export_xlsx(
        "报价漏斗.xlsx",
        ["客户", "报价单号", "名称", "小区/楼盘", "面积(㎡)", "需求类型",
         "综合单价(元/㎡)", "折扣(%)", "报价总额", "状态", "报价日期", "有效期至",
         "明细行数", "流失原因"],
        data, "报价与签单",
    )


# ================================================================== 新增（静态路由）
# 注意：这个必须在 /{qid} 之前，见上方 export 处的说明。

@router.get("/new")
def quote_new(request: Request, customer_id: int = 0,
              db: Session = Depends(get_db), user: User = Depends(login_required)):
    q = Quotation(customer_id=customer_id or 0, quote_date=date.today())
    return render(request, "quotes/form.html", {
        "active": "quotes", "user": user, "obj": None, "draft": q,
        "customers": db.query(Customer).order_by(Customer.name).all(),
        "statuses": QUOTE_STATUS, "categories": QUOTE_CATEGORIES,
        "today": date.today(),
    })


# ================================================================== 详情

@router.get("/{qid}")
def quote_detail(request: Request, qid: int, db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    q = db.get(Quotation, qid)
    if q is None:
        return RedirectResponse("/quotes", status_code=303)
    # 明细行 amount 为 0 时按 数量×单价 现算，页面显示与合计口径一致
    for it in q.items:
        it._amount = float(it.amount or 0) or round(_f(it.qty) * _f(it.unit_price), 2)
    items_total = sum(it._amount for it in q.items)
    return render(request, "quotes/detail.html", {
        "active": "quotes", "user": user, "obj": q, "total": q.total,
        "items_total": round(items_total, 2),
        "categories": QUOTE_CATEGORIES, "statuses": QUOTE_STATUS,
        "projects": db.query(Project).order_by(Project.name).all(),
    })


@router.get("/{qid}/edit")
def quote_edit(request: Request, qid: int, db: Session = Depends(get_db),
               user: User = Depends(login_required)):
    q = db.get(Quotation, qid)
    if q is None:
        return RedirectResponse("/quotes", status_code=303)
    return render(request, "quotes/form.html", {
        "active": "quotes", "user": user, "obj": q, "draft": None,
        "customers": db.query(Customer).order_by(Customer.name).all(),
        "statuses": QUOTE_STATUS, "categories": QUOTE_CATEGORIES,
        "today": date.today(),
    })


@router.post("/save")
def quote_save(request: Request, qid: int = Form(0),
               customer_id: int = Form(0), quote_no: str = Form(""),
               title: str = Form(""), community: str = Form(""),
               area: str = Form("0"), requirement_type: str = Form(""),
               unit_price: str = Form("0"), discount: str = Form("100"),
               status: str = Form("草稿"), quote_date: str = Form(""),
               valid_until: str = Form(""), lost_reason: str = Form(""),
               project_id: int = Form(0), note: str = Form(""),
               db: Session = Depends(get_db), user: User = Depends(login_required)):
    cid = int(customer_id or 0)
    if cid <= 0:
        return RedirectResponse("/quotes/new", status_code=303)
    q = db.get(Quotation, qid) if qid else None
    if q is None:
        q = Quotation(customer_id=cid)
        db.add(q)
    q.customer_id = cid
    q.quote_no = quote_no.strip()
    q.title = title.strip() or "未命名报价"
    q.community = community.strip()
    q.area = _f(area)
    q.requirement_type = requirement_type
    q.unit_price = _f(unit_price)
    q.discount = _f(discount, 100) or 100      # 折扣为 0 时按不打折处理
    q.status = status or "草稿"
    q.quote_date = _d(quote_date) or date.today()
    q.valid_until = _d(valid_until)
    q.lost_reason = lost_reason.strip()
    q.project_id = int(project_id) if project_id else None
    q.note = note.strip()
    db.commit()
    log_audit(db, user, "修改" if qid else "新增", "报价单", q.id,
              f"{q.title} {q.total:,.2f} {q.status}")

    # 成交后把客户状态推进到「已签约」——漏斗和客户列表要一致，
    # 否则会出现「报价单显示已成交、客户列表还显示洽谈中」的自相矛盾。
    if q.status == "已成交":
        c = db.get(Customer, cid)
        if c and c.status not in ("已签约", "已流失"):
            c.status = "已签约"
            db.commit()
    return RedirectResponse(f"/quotes/{q.id}", status_code=303)


@router.post("/{qid}/item")
def quote_item_add(request: Request, qid: int,
                   category: str = Form("其他"), item_name: str = Form(""),
                   qty: str = Form("1"), unit: str = Form("项"),
                   unit_price: str = Form("0"), amount: str = Form(""),
                   note: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    q = db.get(Quotation, qid)
    if q is None:
        return RedirectResponse("/quotes", status_code=303)
    amt = _f(amount) or round(_f(qty) * _f(unit_price), 2)
    db.add(QuotationItem(
        quotation_id=q.id, category=category or "其他",
        item_name=item_name.strip() or "未命名项",
        qty=_f(qty), unit=unit or "项", unit_price=_f(unit_price),
        amount=amt, note=note.strip(),
        sort_order=len(q.items),
    ))
    db.commit()
    return RedirectResponse(f"/quotes/{q.id}", status_code=303)


@router.post("/item/{iid}/delete")
def quote_item_delete(request: Request, iid: int, db: Session = Depends(get_db),
                      user: User = Depends(login_required)):
    it = db.get(QuotationItem, iid)
    if it is None:
        return RedirectResponse("/quotes", status_code=303)
    qid = it.quotation_id
    db.delete(it)
    db.commit()
    return RedirectResponse(f"/quotes/{qid}", status_code=303)


@router.post("/{qid}/status")
def quote_status(request: Request, qid: int, status: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    """一键流转状态（列表页上的快捷操作）。"""
    q = db.get(Quotation, qid)
    if q is None or status not in QUOTE_STATUS:
        return RedirectResponse("/quotes", status_code=303)
    q.status = status
    db.commit()
    log_audit(db, user, "修改", "报价单", q.id, f"状态 -> {status}")
    if status == "已成交":
        c = db.get(Customer, q.customer_id)
        if c and c.status not in ("已签约", "已流失"):
            c.status = "已签约"
            db.commit()
    return RedirectResponse("/quotes", status_code=303)


@router.post("/{qid}/delete")
def quote_delete(request: Request, qid: int, db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    q = db.get(Quotation, qid)
    if q is None:
        return RedirectResponse("/quotes", status_code=303)
    title = q.title
    db.delete(q)
    db.commit()
    log_audit(db, user, "删除", "报价单", qid, title)
    return RedirectResponse("/quotes", status_code=303)