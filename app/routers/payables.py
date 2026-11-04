"""应付账款台账路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import User
from ..payables import by_party, build
from ..utils.excel_export import export_xlsx
from ..web import render

router = APIRouter(tags=["payables"])


@router.get("/payables")
def payables_page(request: Request, kind: str = "all",
                  db: Session = Depends(get_db),
                  user: User = Depends(login_required)):
    """应付账款台账：近期确定要付多少 + 潜在还要付多少。"""
    if kind not in ("all", "purchase", "sub"):
        kind = "all"
    d = build(db, kind)
    return render(request, "payables/list.html", {
        "active": "payables", "user": user, "kind": kind,
        "d": d, "rows": d["rows"], "parties": by_party(d["rows"]),
    })


@router.get("/payables/export")
def payables_export(request: Request, kind: str = "all",
                    db: Session = Depends(get_db),
                    user: User = Depends(login_required)):
    if kind not in ("all", "purchase", "sub"):
        kind = "all"
    d = build(db, kind)
    data = []
    for r in d["rows"]:
        data.append([
            "采购合同" if r["kind"] == "purchase" else "分包合同",
            r["no"], r["name"], r["who"], r["project"],
            r["amount"], r["ordered"], r["paid"], r["due_payable"],
            r["potential"], r["balance"], f'{r["settle_pct"]}%',
            r["invoiced"], r["invoice_gap"],
            str(r["ref_date"] or ""),
        ])
    return export_xlsx(
        "应付账款台账.xlsx",
        ["类型", "合同编号", "合同名称", "供应商/分包商", "项目",
         "合同额", "已下单", "已付", "近期应付(已下单未付)", "潜在应付(未下单)",
         "总敞口(合同-已付)", "付款率", "已开票", "已付未开票", "参考日期"],
        data, "应付账款台账",
    )