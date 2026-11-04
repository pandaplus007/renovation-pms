"""全局搜索：跨客户 / 项目 / 合同 / 材料 / 发票 / 供应商 / 采购单 一键检索。

搜索结果按**当前用户可见的模块**过滤：只有具备对应模块 view 权限的人才搜得到
该类记录，避免「员工」岗位（仅 项目总览 + 项目管理）搜到全公司的发票号与供应商名。
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import (Customer, Invoice, Material, Project, PurchaseContract,
                      PurchaseOrder, SubContract, SubOrder, Supplier, User)
from ..permissions import allowed_modules
from ..web import render

router = APIRouter(prefix="/search", tags=["search"])

# 结果分组：key → (模块, 链接前缀, 名称字段, 图标, 中文名)
GROUPS = [
    ("customers",           "customers",    "/customers/",           "name",      "bi-people",           "客户"),
    ("projects",            "projects",     "/projects/",            "name",      "bi-briefcase",        "项目"),
    ("purchase_contracts",  "procurement",  "/contracts/",           "name",      "bi-file-earmark-text", "采购合同"),
    ("subcontracts",        "subcontracts", "/subcontracts/",        "name",      "bi-diagram-3",        "分包合同"),
    ("materials",           "materials",    "/materials/",           "name",      "bi-box-seam",         "材料"),
    ("invoices",            "invoices",     "/invoices/",            "invoice_no", "bi-receipt",         "发票"),
    ("suppliers",           "suppliers",    "/suppliers/",           "name",      "bi-shop",             "供应商"),
    ("purchase_orders",     "procurement",  "/purchases/",           "order_no",  "bi-cart3",            "采购单"),
    ("sub_orders",          "subcontracts", "/subcontracts/orders/", "order_no",  "bi-cart-check",       "分包采购单"),
]


@router.get("")
def search(request: Request, q: str = "", db: Session = Depends(get_db),
           user: User = Depends(login_required)):
    q = (q or "").strip()
    results = {}
    total = 0
    if q:
        like = f"%{q}%"
        allowed = allowed_modules(user, db)

        def put(key, rows):
            nonlocal total
            mod = next(g[1] for g in GROUPS if g[0] == key)
            if mod not in allowed:
                return
            rows = rows[:20]
            if rows:
                results[key] = rows
                total += len(rows)

        put("customers", db.query(Customer).filter(or_(
            Customer.name.like(like), Customer.contact_person.like(like),
            Customer.contact_phone.like(like), Customer.community.like(like),
            Customer.tax_no.like(like))).all())
        put("projects", db.query(Project).filter(or_(
            Project.name.like(like), Project.address.like(like))).all())
        put("purchase_contracts", db.query(PurchaseContract).filter(or_(
            PurchaseContract.name.like(like), PurchaseContract.contract_no.like(like),
            PurchaseContract.party_b.like(like))).all())
        put("subcontracts", db.query(SubContract).filter(or_(
            SubContract.name.like(like), SubContract.contract_no.like(like),
            SubContract.party_b.like(like))).all())
        put("materials", db.query(Material).filter(or_(
            Material.name.like(like), Material.spec.like(like),
            Material.brand.like(like))).all())
        put("invoices", db.query(Invoice).filter(or_(
            Invoice.invoice_no.like(like), Invoice.title.like(like),
            Invoice.seller_name.like(like))).all())
        put("suppliers", db.query(Supplier).filter(or_(
            Supplier.name.like(like), Supplier.contact.like(like),
            Supplier.phone.like(like))).all())
        put("purchase_orders", db.query(PurchaseOrder).filter(
            PurchaseOrder.order_no.like(like)).all())
        put("sub_orders", db.query(SubOrder).filter(
            SubOrder.order_no.like(like)).all())

    meta = [(k, pre, nf, ic, lb) for k, _m, pre, nf, ic, lb in GROUPS if k in results]
    return render(request, "search.html", {
        "q": q, "results": results, "total": total, "groups": meta,
        "RESULTS_LABELS": {k: lb for k, _m, _p, _n, _i, lb in GROUPS},
        "active": "", "user": user,
    })
