"""银行流水对账：上传对账单 → 自动匹配已有付款 → 找出可能漏记的钱。"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import Supplier, SupplierPayment, User
from ..utils.bank_statement import match_payments, parse_statement
from ..web import render

router = APIRouter(tags=["reconcile"])


@router.get("/reconcile/bank")
def reconcile_page(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(login_required)):
    """上传页：说明支持哪些格式、对账怎么判定。"""
    return render(request, "reconcile/bank.html", {
        "active": "supplier_payments", "user": user, "has_result": False,
    })


def _fmt_date(d):
    if not d:
        return "—"
    if not isinstance(d, date) and hasattr(d, "date"):
        d = d.date()
    return str(d)


@router.post("/reconcile/bank")
async def reconcile_upload(request: Request, file: UploadFile | None = File(None),
                           db: Session = Depends(get_db),
                           user: User = Depends(login_required)):
    """上传银行对账单并立即匹配。"""
    if file is None or not (file.filename or "").strip():
        return render(request, "reconcile/bank.html", {
            "active": "supplier_payments", "user": user, "has_result": False,
            "error": "请选择银行对账单文件。",
        }, status_code=400)
    data = await file.read()
    if not data:
        return render(request, "reconcile/bank.html", {
            "active": "supplier_payments", "user": user, "has_result": False,
            "error": "文件是空的，请重新选择。",
        }, status_code=400)

    items, err = parse_statement(data, file.filename or "")
    if err:
        return render(request, "reconcile/bank.html", {
            "active": "supplier_payments", "user": user, "has_result": False,
            "error": err, "filename": file.filename,
        }, status_code=400)

    res = match_payments(db, items)

    # 展示层统一用纯字典。ORM 对象直接丢给模板有两个隐患：
    #   1. 不能下标赋值（TypeError），只能改属性，等于把展示逻辑写进持久化对象
    #   2. 模板里访问未加载的关系属性会触发额外查询（detached instance 还会报错）
    # 所以在这里一次性转好，模板只碰 dict。
    sup_name = {s.id: (s.name or "") for s in db.query(Supplier).all()}

    matched_rows = []
    for m in res["matched"]:
        p = m["payment"]
        matched_rows.append({
            "score": m["score"], "level": _level(m["score"]),
            "reasons": m["reasons"],
            "date": _fmt_date(m["item"]["date"]),
            "payee": m["item"]["payee"] or "—",
            "summary": m["item"]["summary"] or "",
            "amount": m["item"]["amount"],
            "pay_date": _fmt_date(p.pay_date),
            "supplier": m.get("supplier") or sup_name.get(p.supplier_id) or "未指定",
            "note": p.note or "",
            "payment_id": p.id,
        })

    unmatched_rows = [{
        "date": _fmt_date(it["date"]), "amount": it["amount"],
        "payee": it["payee"] or "—", "summary": it["summary"] or "",
    } for it in res["unmatched_items"]]

    system_only_rows = [{
        "pay_date": _fmt_date(p.pay_date), "amount": float(p.amount or 0),
        "supplier": sup_name.get(p.supplier_id) or "未指定",
        "note": p.note or "", "bank_file": p.bank_file or "",
    } for p in res["unmatched_payments"]]

    return render(request, "reconcile/bank.html", {
        "active": "supplier_payments", "user": user, "has_result": True,
        "filename": file.filename,
        "stats": res["stats"],
        "matched": matched_rows,
        "unmatched_items": unmatched_rows,
        "unmatched_payments": system_only_rows,
    })


def _level(score: int) -> str:
    """流水匹配置信度 → 徽章档位。

    档位名用全站统一的 state-*（见 app.state_colors），不用 Bootstrap 的
    success/info/warning——蓝色在这个系统里没有第二层含义，多一套配色
    只会让「颜色=严重度」这条线索变钝。

        ≥95 高可信 → done（绿）
        ≥70 可疑   → watch（琥珀，需要人看一眼）
        其余       → alert（红，匹配不上就是真问题）
    """
    if score >= 95:
        return "state-done"
    if score >= 70:
        return "state-watch"
    return "state-alert"