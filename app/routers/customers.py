"""客户管理：列表、详情、新增/编辑、跟进记录。"""
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import Customer, FollowUp, User
from ..web import render

router = APIRouter(prefix="/customers", tags=["customers"])


def _num(value: str | None, default=0.0):
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


@router.get("")
def customer_list(request: Request, q: str = "", status: list[str] = Query([]),
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    query = db.query(Customer)
    if q:
        like = f"%{q}%"
        query = query.filter((Customer.name.like(like)) | (Customer.phone.like(like))
                             | (Customer.community.like(like)))
    if status:
        query = query.filter(Customer.status.in_(status))
    customers = query.order_by(Customer.updated_at.desc()).all()

    # 跟进状态：列表上直接标出「该跟进了」，否则这功能等于没有入口——
    # 历史数据里 9 个客户只有 3 条跟进记录，正是因为列表上看不出谁欠跟进。
    from datetime import date as _date
    fu_count = dict(db.query(FollowUp.customer_id, func.count(FollowUp.id))
                    .group_by(FollowUp.customer_id).all())
    next_due = {}
    for f in db.query(FollowUp).filter(FollowUp.next_follow_at.isnot(None)).all():
        d = f.next_follow_at
        # 列声明是 Date，但历史数据里存的是 datetime（两种都出现过）。
        # 统一成 date 再比较，否则 date < datetime 会抛 TypeError 让整页 500。
        if hasattr(d, "date"):
            d = d.date()
        cur = next_due.get(f.customer_id)
        if cur is None or d < cur:
            next_due[f.customer_id] = d
    today = _date.today()
    follow_map = {}
    for c in customers:
        cnt = fu_count.get(c.id, 0)
        due = next_due.get(c.id)
        overdue = bool(due and due < today)
        follow_map[c.id] = {
            "count": cnt,
            "next_at": due,
            "overdue": overdue,
            "days": (today - due).days if overdue else 0,
        }

    return render(request, "customers/list.html", {
        "active": "customers", "user": user, "customers": customers, "q": q, "status": status,
        "follow_map": follow_map,
        "follow_due_count": sum(1 for v in follow_map.values() if v["overdue"]),
        "follow_missing": sum(1 for c in customers
                              if follow_map[c.id]["count"] == 0
                              and c.status not in ("已签约", "已流失")),
    })


def _parse_customer_text(text: str) -> dict:
    """解析外部复制的客户信息。"""
    import re as _re
    r = {"name": "", "tax_no": "", "contact": "", "phone": "", "address": "",
         "community": "", "bank": "", "account": ""}
    m = _re.search(r"[0-9A-HJ-NPQRTUWXY]{18}", text)
    if m:
        r["tax_no"] = m[0]
        text = text.replace(m[0], " ")
    m = _re.search(r"(?:单位电话|联系电话|电话|手机)[：:\s]*([0-9\-\s]{7,15})", text)
    if m:
        r["phone"] = m.group(1).strip().replace(" ", "")
    else:
        m = _re.search(r"1[3-9]\d{9}|0\d{2,3}-?\d{7,8}", text)
        if m:
            r["phone"] = m[0]
    m = _re.search(r"(?:开户银行|开户行|银行名称)[：:\s]*([^\n]{2,60})", text)
    if m:
        r["bank"] = m.group(1).strip()
    m = _re.search(r"(?:银行账户|银行账号|账号)[：:\s]*([0-9\s-]{10,30})", text)
    if m:
        r["account"] = m.group(1).replace(" ", "")
    m = _re.search(r"[^\n]{0,40}(?:有限公司|有限责任公司|集团有限公司|公司|集团|中心|商行|工厂|建材店|经销部)[^\n]{0,40}", text)
    if m:
        r["name"] = m.group(0).strip()
    m = _re.search(r"(?:联系人|负责人|法人|姓名|客户)[：:\s]*([\u4e00-\u9fa5]{2,4})", text)
    if m:
        r["contact"] = m.group(1)
    m = _re.search(r"(?:地址|住所|注册地址|公司地址)[：:\s]*([^\n，,。；;]{5,60})", text)
    if m:
        r["address"] = m.group(1).strip()
    m = _re.search(r"[^\n]{0,20}(?:小区|花园|华庭|名苑|城|苑|湾|府)[^\n]{0,20}", text)
    if m and m.group(0).strip() not in r["name"]:
        r["community"] = m.group(0).strip()
    return r


@router.post("/parse")
def customer_parse(request: Request, text: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    """粘贴识别：解析客户信息，预填新增表单。"""
    parsed = _parse_customer_text(text or "")
    if not any(parsed.values()):
        request.session["customer_parsed"] = {"error": "未能从粘贴内容中识别到客户信息，请检查格式"}
    else:
        request.session["customer_parsed"] = parsed
    return RedirectResponse(url="/customers/new", status_code=303)


@router.get("/new")
def customer_new(request: Request, user: User = Depends(login_required)):
    parsed = request.session.pop("customer_parsed", None)
    return render(request, "customers/form.html", {
        "active": "customers", "user": user, "customer": None, "parsed": parsed,
    })


@router.post("/new")
def customer_create(request: Request,
                    name: str = Form(...), tax_no: str = Form(""),
                    contact_person: str = Form(""), contact_phone: str = Form(""),
                    company_address: str = Form(""),
                    bank_name: str = Form(""), bank_account: str = Form(""),
                    phone: str = Form(""), community: str = Form(""),
                    house_type: str = Form(""), area: float = Form(0), requirement_type: str = Form(""),
                    budget_low: float = Form(0), budget_high: float = Form(0), source: str = Form(""),
                    status: str = Form("线索"), note: str = Form(""),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    customer = Customer(name=name, tax_no=tax_no, contact_person=contact_person,
                        contact_phone=contact_phone, company_address=company_address,
                        bank_name=bank_name, bank_account=bank_account,
                        phone=phone, community=community, house_type=house_type,
                        area=area, requirement_type=requirement_type,
                        budget_low=budget_low, budget_high=budget_high,
                        source=source, status=status, note=note)
    db.add(customer)
    db.commit()
    return RedirectResponse(url=f"/customers/{customer.id}", status_code=303)


@router.get("/{customer_id}")
def customer_detail(customer_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    customer = db.get(Customer, customer_id)
    if customer is None:
        return RedirectResponse(url="/customers", status_code=303)
    return render(request, "customers/detail.html", {
        "active": "customers", "user": user, "customer": customer,
    })


@router.get("/{customer_id}/edit")
def customer_edit(customer_id: int, request: Request,
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    customer = db.get(Customer, customer_id)
    if customer is None:
        return RedirectResponse(url="/customers", status_code=303)
    return render(request, "customers/form.html", {
        "active": "customers", "user": user, "customer": customer,
    })


@router.post("/{customer_id}/edit")
def customer_update(customer_id: int, request: Request,
                    name: str = Form(...), tax_no: str = Form(""),
                    contact_person: str = Form(""), contact_phone: str = Form(""),
                    company_address: str = Form(""),
                    bank_name: str = Form(""), bank_account: str = Form(""),
                    phone: str = Form(""), community: str = Form(""),
                    house_type: str = Form(""), area: float = Form(0), requirement_type: str = Form(""),
                    budget_low: float = Form(0), budget_high: float = Form(0), source: str = Form(""),
                    status: str = Form("线索"), note: str = Form(""),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    customer = db.get(Customer, customer_id)
    if customer is None:
        return RedirectResponse(url="/customers", status_code=303)
    customer.name = name
    customer.tax_no = tax_no
    customer.contact_person = contact_person
    customer.contact_phone = contact_phone
    customer.company_address = company_address
    customer.bank_name = bank_name
    customer.bank_account = bank_account
    customer.phone = phone
    customer.community = community
    customer.house_type = house_type
    customer.area = area
    customer.requirement_type = requirement_type
    customer.budget_low = budget_low
    customer.budget_high = budget_high
    customer.source = source
    customer.status = status
    customer.note = note
    db.commit()
    return RedirectResponse(url=f"/customers/{customer.id}", status_code=303)


@router.post("/{customer_id}/follow-ups")
def follow_up_create(customer_id: int, request: Request,
                     content: str = Form(...), next_follow_at: str = Form(""),
                     db: Session = Depends(get_db), user: User = Depends(login_required)):
    customer = db.get(Customer, customer_id)
    if customer is None:
        return RedirectResponse(url="/customers", status_code=303)
    follow_up = FollowUp(customer_id=customer_id, content=content)
    if next_follow_at:
        from datetime import date
        try:
            follow_up.next_follow_at = date.fromisoformat(next_follow_at)
        except ValueError:
            pass
    db.add(follow_up)
    db.commit()
    return RedirectResponse(url=f"/customers/{customer_id}", status_code=303)


@router.post("/{customer_id}/delete")
def customer_delete(customer_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    customer = db.get(Customer, customer_id)
    if customer is None:
        return RedirectResponse(url="/customers", status_code=303)
    if customer.projects:
        # 有关联项目不允许直接删除，避免误删项目数据
        return render(request, "customers/detail.html", {
            "active": "customers", "user": user, "customer": customer,
            "error": "该客户已关联项目，请先处理关联项目后再删除。",
        })
    db.delete(customer)
    db.commit()
    return RedirectResponse(url="/customers", status_code=303)
