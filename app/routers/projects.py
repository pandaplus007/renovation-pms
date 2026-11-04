"""项目管理：列表、详情、新建/编辑、阶段、收款、预算成本、材料、任务、现场、文档。"""
import json
import os
import urllib.parse
from datetime import date

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import changes
from ..auth import login_required
from ..config import UPLOAD_DIR
from ..database import get_db
from ..models import (
    BudgetItem, Contract, CostItem, Customer, Document, Invoice, Material, Payment,
    PaymentReceipt, Phase, Project, ProjectLedger, ProjectMaterial, ProjectNote, ProjectPhase,
    PurchaseContract, PurchaseOrder,
    PurchaseOrderItem, SiteRecord, SiteRecordFollowup, Staff, SubContract, SubOrder, Supplier, SupplierPayment, Task, User,
)
from .reports import _cost_data
from ..approvals import filter_booked
from ..utils.audit import log_audit
from ..utils.bank_parser import recognize_bank_file
from ..utils.excel_export import export_xlsx
from ..utils.excel_import import parse_budget_excel
from ..utils.contract_parser import recognize_contract_file
from ..utils.files import DOC_EXTS, IMAGE_EXTS, save_upload
from ..row_scope import SCOPE_LABELS, can_see_project, project_scope_filter, user_scope
from ..voided import VOIDED, VOID_ATTRS, mark_voided, not_voided, restore
from ..web import render

router = APIRouter(prefix="/projects", tags=["projects"])


def _f(v) -> float:
    try:
        return float(str(v).replace(",", "").strip()) if str(v).strip() else 0
    except (ValueError, TypeError):
        return 0


def _date(v: str):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _d(v):
    try:
        return date.fromisoformat(str(v).replace(".", "-").replace("/", "-"))
    except ValueError:
        return None


def _staff_list(db: Session):
    """人员名册（供负责人 / 设计师 / 工长 / 监理下拉用）。

    传全部（含已停用），由模板的宏按场景决定是否显示已停用的：
    新建时不该选到离职的人，编辑老记录时必须能看到原来的那个人，
    否则一保存就把负责人清空了。宏里 include_inactive 控制这件事。
    """
    return db.query(Staff).order_by(Staff.sort_order, Staff.id).all()


def recalc_progress(db: Session, project: Project):
    """根据阶段完成情况自动计算项目进度。"""
    total = len(project.phases)
    done = sum(1 for ph in project.phases if ph.status == "已完成")
    project.progress = int(done / total * 100) if total else project.progress
    db.commit()


# ---------- 列表 ----------
@router.get("")
def project_list(request: Request, q: str = "", status: list[str] = Query([]),
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    # 行级权限先套在最前面：后面所有 filter 都建立在「已按范围收窄」的查询上。
    # 反过来做（先查完再在 Python 里剔除）既慢又容易漏。
    query = db.query(Project)
    scope_cond = project_scope_filter(user, Project)
    if scope_cond is not None:
        query = query.filter(scope_cond)
    if q:
        like = f"%{q}%"
        query = query.join(Project.customer).filter(
            (Project.name.like(like)) | (Customer.name.like(like)))
    if status:
        query = query.filter(Project.status.in_(status))
    projects = query.order_by(Project.updated_at.desc()).all()

    # 成本报表数据（合并到项目管理页）。传 user 让汇总数字也跟着收窄——
    # 列表只有 3 个项目、汇总却写着全公司合同额，等于没做权限。
    rows, total_contract, total_received, total_cost, profit_total, profit_rate = _cost_data(db, user)
    # 分类成本小计也要按范围收窄：CostItem 通过 project_id 关联，
    # 这里用可见项目 id 集合过滤，而不是全表求和。
    _vis_ids = {p.id for p in projects}
    by_category: dict[str, float] = {}
    for c in filter_booked(db, "cost_item",
                           db.query(CostItem).filter(
                               not_voided(CostItem),
                               CostItem.project_id.in_(_vis_ids) if _vis_ids else False).all()):
        by_category[c.category] = by_category.get(c.category, 0) + c.amount

    return render(request, "projects/list.html", {
        "active": "projects", "user": user, "projects": projects, "q": q, "status": status,
        "rows": rows, "total_contract": total_contract, "total_received": total_received,
        "total_cost": total_cost, "profit_total": profit_total, "profit_rate": profit_rate,
        "by_category": sorted(by_category.items(), key=lambda x: -x[1]),
        "data_scope": user_scope(user),
        "SCOPE_LABELS": SCOPE_LABELS,
    })


# ---------- 新建 ----------
@router.post("/parse-contract")
async def project_new_parse_contract(request: Request, file: UploadFile | None = File(None),
                                     db: Session = Depends(get_db),
                                     user: User = Depends(login_required)):
    """新建项目页：上传施工合同 → 识别 → 预填合同字段。"""
    if not file or not file.filename:
        payload = {"error": "请选择合同文件"}
    else:
        data = await file.read()
        file.file.seek(0)
        file_path = await save_upload(file, "contract", allowed={".pdf", ".docx"})
        parsed, parse_err = recognize_contract_file(file.filename, data)
        if parsed:
            parsed["file_path"] = file_path or ""
            payload = json.loads(json.dumps(parsed, default=str))
        else:
            payload = {"error": parse_err or "未能识别该合同（可能是扫描件），可手动填写合同信息",
                       "file_path": file_path or ""}
    try:
        with open(_new_ct_path(), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        request.session["new_contract_parsed"] = payload
    return RedirectResponse(url="/projects/new#contract-zone", status_code=303)


@router.get("/new")
def project_new(request: Request, db: Session = Depends(get_db),
                user: User = Depends(login_required)):
    customers = db.query(Customer).all()
    cp = None
    try:
        with open(_new_ct_path(), encoding="utf-8") as f:
            cp = json.load(f)
        try:
            os.remove(_new_ct_path())
        except Exception:
            pass
    except FileNotFoundError:
        cp = request.session.pop("new_contract_parsed", None)
    except Exception:
        cp = request.session.pop("new_contract_parsed", None)
    cp_pay_terms = ""
    if cp and cp.get("pay_terms"):
        cp_pay_terms = json.dumps(cp["pay_terms"], ensure_ascii=False)
    return render(request, "projects/form.html", {
        "active": "projects", "user": user, "project": None, "customers": customers,
        "selected_customer_id": None, "contract_parsed": cp, "cp_pay_terms": cp_pay_terms,
        "staffs": _staff_list(db),
    })


@router.post("/new")
def project_create(request: Request,
                   customer_id: int = Form(...), name: str = Form(...), address: str = Form(""),
                   area: float = Form(0), style: str = Form(""), start_date: str = Form(""),
                   end_date: str = Form(""), status: str = Form("待签约"), progress: int = Form(0),
                   designer: str = Form(""), foreman: str = Form(""), supervisor: str = Form(""),
                   note: str = Form(""), contract_amount: float = Form(0), sign_date: str = Form(""),
                   ct_no: str = Form(""), ct_name: str = Form(""),
                   ct_party_a: str = Form(""), ct_party_b: str = Form(""),
                   ct_amount: float = Form(0), ct_sign_date: str = Form(""),
                   ct_start_date: str = Form(""), ct_end_date: str = Form(""),
                   ct_duration: int = Form(0), ct_tax_rate: float = Form(0),
                   ct_prepay: float = Form(0), ct_warranty: int = Form(0),
                   ct_warranty_rate: float = Form(0), ct_pay_method: str = Form(""),
                   ct_file: str = Form(""), ct_pay_terms: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    project = Project(customer_id=customer_id, name=name, address=address, area=area,
                      style=style, start_date=_date(start_date), end_date=_date(end_date),
                      status=status, progress=progress, designer=designer, foreman=foreman,
                      supervisor=supervisor, note=note)
    db.add(project)
    db.flush()
    # 施工合同（新建项目时导入识别，随项目创建）
    if ct_name or ct_amount or ct_party_a or contract_amount:
        if not ct_amount:
            ct_amount = contract_amount
        customer_name = ""
        if customer_id:
            _cus = db.get(Customer, customer_id)
            if _cus:
                customer_name = _cus.name or ""
        if not ct_party_a:
            ct_party_a = customer_name
        if not ct_party_b:
            ct_party_b = _company_name()
        if not ct_name:
            ct_name = name
        pay_terms_json = ct_pay_terms or ""
        if not pay_terms_json:
            try:
                with open(_new_ct_path(), encoding="utf-8") as _f:
                    _np = json.load(_f)
                if _np.get("pay_terms"):
                    pay_terms_json = json.dumps(_np["pay_terms"], ensure_ascii=False)
            except Exception:
                pass
        db.add(Contract(
            project_id=project.id, contract_no=ct_no, name=ct_name,
            party_a=ct_party_a, party_b=ct_party_b or "",
            amount=ct_amount, sign_date=_date(ct_sign_date or sign_date),
            start_date=_date(ct_start_date), end_date=_date(ct_end_date),
            duration_days=ct_duration, tax_rate=ct_tax_rate,
            prepay_rate=ct_prepay, warranty_months=ct_warranty,
            warranty_retention_rate=ct_warranty_rate, pay_method=ct_pay_method,
            pay_terms=pay_terms_json, file_path=ct_file))
        # 付款节点 → 收款计划（仅首次：项目无收款节点时）
        if pay_terms_json:
            try:
                _terms = json.loads(pay_terms_json)
            except Exception:
                _terms = []
            if _terms and not db.query(Payment).filter(Payment.project_id == project.id).first():
                for _t in _terms:
                    _amt = float(_t.get("amount_wan") or 0) * 10000 or (ct_amount * float(_t.get("rate") or 0) / 100)
                    if _amt <= 0:
                        continue
                    db.add(Payment(project_id=project.id, name=_t.get("name", ""),
                                   plan_amount=round(_amt, 2),
                                   plan_date=_d(_t.get("pay_date"))))
        try:
            if os.path.exists(_new_ct_path()):
                os.remove(_new_ct_path())
        except Exception:
            pass
    # 自动复制阶段模板
    for ph in db.query(Phase).order_by(Phase.sort_order).all():
        db.add(ProjectPhase(project_id=project.id, name=ph.name, sort_order=ph.sort_order, status="未开始"))
    # 客户签约状态联动
    customer = db.get(Customer, customer_id)
    if customer and status != "待签约":
        customer.status = "已签约"
    db.commit()
    return RedirectResponse(url=f"/projects/{project.id}", status_code=303)


# ---------- 详情 ----------
LEDGER_CATEGORIES = ["到账", "其他收入", "材料付款", "劳务付款", "成本费用", "税费扣费", "付个人", "其他支出"]
LEDGER_INCOME = {"到账", "其他收入"}
LEDGER_EXPENSE = {"材料付款", "劳务付款", "成本费用", "税费扣费", "付个人", "其他支出"}


def _ledger_data(db: Session, project_id: int) -> dict:
    """项目资金台账：自动联动（收款/付款/费用）+ 手动流水，返回流水行、汇总、进项发票。

    付款与费用会经过 `approvals.filter_booked`：若该类型启用了「未审批不入账」，
    未获审批通过的单据不生成流水行，也就不计入收支汇总。
    """
    rows = []
    # 到账（收款）——排除作废节点，否则台账收支对不平
    for p in db.query(Payment).filter(Payment.project_id == project_id,
                                      Payment.paid_amount > 0,
                                      not_voided(Payment, VOID_ATTRS["payments"])).order_by(
            Payment.sort_order, Payment.id).all():
        rows.append({"date": p.paid_date, "summary": f"收款·{p.name}", "category": "到账",
                     "amount": round(p.paid_amount or 0, 2), "source": "payment", "id": p.id})
    # 材料付款（关联采购合同/采购单，未关联分包）
    _mat = filter_booked(db, "supplier_payment", db.query(SupplierPayment).filter(
        SupplierPayment.project_id == project_id,
        SupplierPayment.subcontract_id.is_(None),
        SupplierPayment.suborder_id.is_(None),
        not_voided(SupplierPayment)).all())
    for sp in _mat:
        if sp.contract_id or sp.po_id:
            _sup = sp.supplier.name if sp.supplier else "供应商"
            rows.append({"date": sp.pay_date, "summary": f"材料款·{_sup}", "category": "材料付款",
                         "amount": -round(sp.amount or 0, 2), "source": "supplier_payment", "id": sp.id})
    # 劳务付款（关联分包合同/分包单）
    _lab = filter_booked(db, "supplier_payment", db.query(SupplierPayment).filter(
        SupplierPayment.project_id == project_id,
        (SupplierPayment.subcontract_id.isnot(None)) | (SupplierPayment.suborder_id.isnot(None)),
        not_voided(SupplierPayment)).all())
    for sp in _lab:
        _sup = sp.supplier.name if sp.supplier else "供应商"
        rows.append({"date": sp.pay_date, "summary": f"劳务款·{_sup}", "category": "劳务付款",
                     "amount": -round(sp.amount or 0, 2), "source": "supplier_payment", "id": sp.id})
    # 成本费用
    for c in filter_booked(db, "cost_item", db.query(CostItem).filter(
            CostItem.project_id == project_id, not_voided(CostItem)).all()):
        rows.append({"date": c.occurred_date, "summary": c.item_name or "费用", "category": "成本费用",
                     "amount": -round(c.amount or 0, 2), "source": "cost", "id": c.id})
    # 手动流水
    for l in db.query(ProjectLedger).filter(ProjectLedger.project_id == project_id).all():
        rows.append({"date": l.entry_date, "summary": l.summary, "category": l.category,
                     "amount": round(l.amount or 0, 2), "source": "manual", "id": l.id, "note": l.note})
    rows.sort(key=lambda r: (r["date"] or date.min, 0 if r["source"] == "manual" else 1, r["id"]))

    by_cat = {}
    for r in rows:
        by_cat[r["category"]] = round(by_cat.get(r["category"], 0) + r["amount"], 2)
    income_total = round(sum(r["amount"] for r in rows if r["amount"] > 0), 2)
    expense_total = round(sum(-r["amount"] for r in rows if r["amount"] < 0), 2)
    received = round(by_cat.get("到账", 0) + by_cat.get("其他收入", 0), 2)
    material = round(-by_cat.get("材料付款", 0), 2)
    labor = round(-by_cat.get("劳务付款", 0), 2)
    cost = round(-by_cat.get("成本费用", 0), 2)
    tax = round(-by_cat.get("税费扣费", 0), 2)
    summary = {
        "income_total": income_total, "expense_total": expense_total,
        "received": received, "material": material, "labor": labor, "cost": cost, "tax": tax,
        "balance": round(income_total - expense_total, 2),
        "profit": round(received - material - labor - cost, 2),
    }

    # 进项发票（材料发票 + 退税）
    invoices_in = db.query(Invoice).filter(
        Invoice.project_id == project_id, Invoice.direction == "进项",
        Invoice.status != "作废").order_by(Invoice.invoice_date.is_(None), Invoice.invoice_date).all()
    for i in invoices_in:
        # 不含税价：优先取已填值，否则自动计算 = 价税合计 - 税额
        i.calc_tax_excl = round(i.tax_excl if i.tax_excl else ((i.amount or 0) - (i.tax_amount or 0)), 2)
    inv_totals = {
        "tax_excl": round(sum(i.calc_tax_excl for i in invoices_in), 2),
        "tax": round(sum(i.tax_amount or 0 for i in invoices_in), 2),
        "amount": round(sum(i.amount or 0 for i in invoices_in), 2),
    }
    # 销项（开票）合计
    invoice_out = db.query(Invoice).filter(
        Invoice.project_id == project_id, Invoice.direction == "销项",
        Invoice.status != "作废").all()
    out_total = round(sum(i.amount or 0 for i in invoice_out), 2)

    return {"rows": rows, "summary": summary, "by_cat": by_cat,
            "invoices_in": invoices_in, "inv_totals": inv_totals,
            "invoice_out_total": out_total}


@router.get("/{project_id}")
def project_detail(project_id: int, request: Request,
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    project = db.get(Project, project_id)
    if project is None:
        return RedirectResponse(url="/projects", status_code=303)
    # 行级权限二次校验：**不能只靠列表页过滤**——URL 里的 id 是可以手输的，
    # 侧栏项目树、历史书签、企微推送里的链接都会绕过列表。
    # 这里返回 404 而不是 403：403 泄露「这个项目存在」，404 不泄露任何信息。
    if not can_see_project(user, project, Project):
        raise HTTPException(status_code=404, detail="项目不存在")
    import_msg = request.query_params.get("import_msg", "")
    # 上传合同 / 保存备注的提示：一次性消费（项目详情页没有全局 flash 区）
    contract_msg = request.session.pop("contract_msg", None)
    budget_total = sum(b.budget_amount for b in project.budget_items)
    po_list = db.query(PurchaseOrder).filter(PurchaseOrder.project_id == project_id).all()
    po_total = sum(po.total_amount for po in po_list)
    subcontracts = db.query(SubContract).filter(SubContract.project_id == project_id).all()
    sub_orders_total = sum(o.total_amount for sc in subcontracts for o in sc.orders)
    cost_items_total = sum(c.amount for c in project.cost_items)
    # 成本合计 = 分包采购单 + 采购单 + 费用管理
    cost_total = round(cost_items_total + po_total + sub_orders_total, 2)
    # 签订合同金额 = 采购合同额 + 分包合同额（该项目）
    signed_purchase_total = sum(c.amount or 0 for c in db.query(PurchaseContract).filter(
        PurchaseContract.project_id == project_id).all())
    signed_sub_total = sum(sc.amount or 0 for sc in subcontracts)
    contract_signed_total = round(signed_purchase_total + signed_sub_total, 2)
    # 已下单总金额 = 采购单 + 分包采购单
    ordered_total = round(po_total + sub_orders_total, 2)
    contract = project.contract
    contract_total = contract.amount if contract else 0
    # 手动补录面板要回填付款节点
    contract_pay_terms = []
    if contract and contract.pay_terms:
        try:
            contract_pay_terms = json.loads(contract.pay_terms)
        except Exception:
            contract_pay_terms = []
    paid_total = sum(p.paid_amount for p in project.payments
                     if getattr(p, "void_status", "正常") != VOIDED)
    materials = db.query(Material).order_by(Material.name).all()
    po_list = db.query(PurchaseOrder).filter(PurchaseOrder.project_id == project_id).all()
    invoices = db.query(Invoice).filter(Invoice.project_id == project_id).all()
    supplier_payments = filter_booked(db, "supplier_payment", db.query(SupplierPayment).filter(
        SupplierPayment.project_id == project_id, not_voided(SupplierPayment)).all())
    supplier_paid_total = sum(p.amount for p in supplier_payments)
    # 已下单未付款 = 已下单总金额 - 已付款总金额
    ordered_unpaid = round(ordered_total - supplier_paid_total, 2)
    # 发票与应付统计
    invoice_out_total = sum(i.amount for i in invoices
                            if i.direction == "销项" and i.status != "作废")
    invoice_in_total = sum(i.amount for i in invoices
                           if i.direction == "进项" and i.status != "作废")
    po_total = sum(po.total_amount for po in po_list)
    supplier_balance = round(po_total - supplier_paid_total, 2)
    suppliers = db.query(Supplier).order_by(Supplier.name).all()

    # 预算 vs 成本对比（按预算类别聚合）
    budget_by_cat: dict[str, float] = {cat: 0.0 for cat in
                                       ["主材", "辅材", "人工", "设计", "管理", "其他", "分包"]}
    # 分包成本：该项目分包合同的采购单金额，按费用分类统计
    sub_by_cat: dict[str, float] = {}
    for sc in db.query(SubContract).filter(SubContract.project_id == project_id).all():
        _amt = sum(o.total_amount for o in sc.orders)
        if _amt > 0:
            sub_by_cat[sc.category or "其他"] = sub_by_cat.get(sc.category or "其他", 0) + _amt
    sub_total_all = sum(sub_by_cat.values())                     # 全部分包（含劳务分包）
    labor_sub = sub_by_cat.get("劳务分包", 0)                     # 劳务分包
    sub_total = sub_total_all - labor_sub                        # 分包合计：不含劳务分包（劳务在人工中）
    for b in project.budget_items:
        budget_by_cat[b.category] = budget_by_cat.get(b.category, 0) + b.budget_amount
    material_cost = db.query(
        func.coalesce(func.sum(PurchaseOrderItem.quantity * PurchaseOrderItem.unit_price), 0.0)
    ).join(PurchaseOrder, PurchaseOrderItem.po_id == PurchaseOrder.id).filter(
        PurchaseOrder.project_id == project_id).scalar() or 0
    cost_by_cat: dict[str, float] = {cat: 0.0 for cat in
                                     ["人工", "机械", "设计", "管理", "运输", "辅材", "其他费用"]}
    # 按分类聚合前先剔除未审批通过的费用（未审批不入账）与已作废的费用，
    # 否则这里会是 SQL 聚合、无法逐行过滤，改为内存聚合
    _booked_costs = filter_booked(db, "cost_item", db.query(CostItem).filter(
        CostItem.project_id == project_id, not_voided(CostItem)).all())
    for c in _booked_costs:
        cost_by_cat[c.category] = cost_by_cat.get(c.category, 0) + (c.amount or 0)
    cat_sources = {
        "主材": ("材料采购（材料管理）", material_cost),
        "辅材": ("费用管理-辅材", cost_by_cat.get("辅材", 0)),
        "人工": ("费用管理-人工 + 分包劳务", cost_by_cat.get("人工", 0) + labor_sub),
        "设计": ("已计入分包合同，不重复统计", 0),
        "管理": ("费用管理-管理", cost_by_cat.get("管理", 0)),
        "其他": ("费用管理-机械+运输+其他 + 分包其他",
                cost_by_cat.get("机械", 0) + cost_by_cat.get("运输", 0)
                + cost_by_cat.get("其他费用", 0)
                + sub_by_cat.get("专业分包", 0) + sub_by_cat.get("机械租赁", 0) + sub_by_cat.get("其他", 0)),
        "分包": ("分包采购单（不含劳务分包）", sub_total),
    }
    budget_compare = []
    for cat in ["主材", "辅材", "人工", "管理", "其他", "分包"]:
        bgt = budget_by_cat.get(cat, 0)
        src, cst = cat_sources[cat]
        budget_compare.append({"cat": cat, "budget": bgt, "cost": cst,
                               "source": src,
                               "diff": (bgt - cst) if cst is not None else None,
                               "rate": (round(cst / bgt * 100, 1) if bgt else 0)
                               if cst is not None else None,
                               "sub_detail": ({k: v for k, v in sub_by_cat.items() if k != "劳务分包"}
                                               if cat == "分包" else None)})
    compare_budget_total = sum(r["budget"] for r in budget_compare)
    compare_cost_total = (material_cost
                          + (sum(cost_by_cat.values()) - cost_by_cat.get("设计", 0))
                          + sub_total_all)
    ledger = _ledger_data(db, project_id)
    # 变更签证摘要（项目详情页的「变更签证」标签用）
    change_summary = changes.project_change_totals(db)
    change_settle = changes.settle_amount(db, project_id, contract)
    # 任务负责人下拉：只列启用账号（停用的不该再被派活）。
    # 传全部用户也行，但下拉里混着已停用账号时，员工会派给一个登不进去的人。
    task_users = db.query(User).filter(User.is_active.is_(True)).order_by(
        User.display_name, User.username).all()
    return render(request, "projects/detail.html", {
        "active": "projects", "user": user, "project": project,
        "contract": contract,
        "users": task_users,
        # 任务负责人 / 设计师 / 工长 下拉的候选
        "staffs": _staff_list(db),
        "change_summary": change_summary, "change_settle": change_settle,
        "budget_total": budget_total, "cost_total": cost_total,
        "contract_total": contract_total, "paid_total": paid_total,
        "materials": materials, "po_list": po_list, "suppliers": suppliers,
        "invoices": invoices, "supplier_payments": supplier_payments,
        "supplier_paid_total": supplier_paid_total,
        "invoice_out_total": invoice_out_total, "invoice_in_total": invoice_in_total,
        "po_total": po_total, "supplier_balance": supplier_balance,
        "contract_signed_total": contract_signed_total, "ordered_total": ordered_total,
        "ordered_unpaid": ordered_unpaid,
        "budget_compare": budget_compare,
        "compare_budget_total": compare_budget_total,
        "compare_cost_total": compare_cost_total,
        "material_cost": material_cost,
        "import_msg": import_msg,
        "contract_msg": contract_msg,
        "contract_pay_terms": contract_pay_terms,
        "today": date.today(),
        "ledger": ledger,
        "ledger_categories": LEDGER_CATEGORIES,
    })


# ---------- 项目台账：手动流水 CRUD ----------
def _ledger_back(back_url: str, default_url: str):
    if back_url and back_url.startswith("/") and not back_url.startswith("//"):
        return RedirectResponse(url=back_url, status_code=303)
    return RedirectResponse(url=default_url, status_code=303)


@router.post("/{project_id}/ledger")
def project_ledger_create(project_id: int, request: Request,
                          entry_date: str = Form(""), summary: str = Form(""),
                          category: str = Form("其他支出"), amount: float = Form(0),
                          note: str = Form(""), back_url: str = Form(""),
                          db: Session = Depends(get_db), user: User = Depends(login_required)):
    project = db.get(Project, project_id)
    if project is None:
        return RedirectResponse(url="/projects", status_code=303)
    _amt = abs(amount or 0)
    if _amt <= 0:
        return _ledger_back(back_url, f"/projects/{project_id}#ledger")
    if category in LEDGER_EXPENSE:
        _amt = -_amt
    db.add(ProjectLedger(project_id=project_id, entry_date=_date(entry_date), summary=summary.strip(),
                         category=category, amount=round(_amt, 2), note=note.strip()))
    db.commit()
    return _ledger_back(back_url, f"/projects/{project_id}#ledger")


@router.post("/ledger/{ledger_id}/delete")
def project_ledger_delete(ledger_id: int, request: Request, back_url: str = Form(""),
                          db: Session = Depends(get_db), user: User = Depends(login_required)):
    l = db.get(ProjectLedger, ledger_id)
    if l:
        pid = l.project_id
        db.delete(l)
        db.commit()
        return _ledger_back(back_url, f"/projects/{pid}#ledger")
    return RedirectResponse(url="/projects", status_code=303)


@router.post("/ledger/{ledger_id}/edit")
def project_ledger_update(ledger_id: int, request: Request,
                          entry_date: str = Form(""), summary: str = Form(""),
                          category: str = Form("其他支出"), amount: float = Form(0),
                          note: str = Form(""), back_url: str = Form(""),
                          db: Session = Depends(get_db), user: User = Depends(login_required)):
    l = db.get(ProjectLedger, ledger_id)
    if l is None:
        return RedirectResponse(url="/projects", status_code=303)
    _amt = abs(amount or 0)
    if category in LEDGER_EXPENSE:
        _amt = -_amt
    l.entry_date = _date(entry_date)
    l.summary = summary.strip()
    l.category = category
    l.amount = round(_amt, 2)
    l.note = note.strip()
    db.commit()
    return _ledger_back(back_url, f"/projects/{l.project_id}#ledger")


# ---------- 材料发票退税字段编辑 ----------
@router.post("/invoices/{invoice_id}/refund")
def invoice_refund_update(invoice_id: int, request: Request,
                          tax_excl: float = Form(-1), refund_status: str = Form(""),
                          back_url: str = Form(""),
                          db: Session = Depends(get_db), user: User = Depends(login_required)):
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        return RedirectResponse(url="/projects", status_code=303)
    if tax_excl >= 0:
        inv.tax_excl = round(tax_excl, 2)
        if tax_excl > 0 and not inv.tax_amount:
            inv.tax_amount = round(inv.amount - tax_excl, 2)
    if refund_status:
        inv.refund_status = refund_status
    db.commit()
    _pid = inv.project_id
    return _ledger_back(back_url, f"/projects/{_pid}#ledger" if _pid else "/invoices")


@router.post("/{project_id}/invoices/batch-refund")
def invoice_batch_refund(project_id: int, request: Request,
                         invoice_ids: list[int] = Form([]),
                         tax_excl: float = Form(-1), refund_status: str = Form(""),
                         back_url: str = Form(""),
                         db: Session = Depends(get_db), user: User = Depends(login_required)):
    """批量修改进项发票的不含税价/退税状态。"""
    for iid in invoice_ids:
        inv = db.get(Invoice, iid)
        if inv is None:
            continue
        if tax_excl >= 0:
            inv.tax_excl = round(tax_excl, 2)
            if tax_excl > 0 and not inv.tax_amount:
                inv.tax_amount = round(inv.amount - tax_excl, 2)
        if refund_status:
            inv.refund_status = refund_status
    db.commit()
    return _ledger_back(back_url, f"/projects/{project_id}#ledger")


# ---------- 编辑 ----------
@router.get("/{project_id}/edit")
def project_edit(project_id: int, request: Request,
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    project = db.get(Project, project_id)
    if project is None:
        return RedirectResponse(url="/projects", status_code=303)
    customers = db.query(Customer).all()
    return render(request, "projects/form.html", {
        "active": "projects", "user": user, "project": project, "customers": customers,
        "selected_customer_id": project.customer_id,
        "staffs": _staff_list(db),
    })


@router.post("/{project_id}/edit")
def project_update(project_id: int, request: Request,
                   customer_id: int = Form(...), name: str = Form(...), address: str = Form(""),
                   area: float = Form(0), style: str = Form(""), start_date: str = Form(""),
                   end_date: str = Form(""), status: str = Form("待签约"), progress: int = Form(0),
                   designer: str = Form(""), foreman: str = Form(""), supervisor: str = Form(""),
                   note: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    project = db.get(Project, project_id)
    if project is None:
        return RedirectResponse(url="/projects", status_code=303)
    project.customer_id = customer_id
    project.name = name
    project.address = address
    project.area = area
    project.style = style
    project.start_date = _date(start_date)
    project.end_date = _date(end_date)
    project.status = status
    project.progress = progress
    project.designer = designer
    project.foreman = foreman
    project.supervisor = supervisor
    project.note = note
    customer = db.get(Customer, customer_id)
    if customer and status != "待签约":
        customer.status = "已签约"
    db.commit()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/delete")
def project_delete(project_id: int, request: Request,
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    project = db.get(Project, project_id)
    if project:
        db.delete(project)
        db.commit()
    return RedirectResponse(url="/projects", status_code=303)


# ---------- 阶段 ----------
@router.post("/{project_id}/phases")
def phase_update(project_id: int, request: Request,
                 phase_id: int = Form(...), status: str = Form("未开始"),
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    ph = db.get(ProjectPhase, phase_id)
    if ph and ph.project_id == project_id:
        ph.status = status if status in ("未开始", "进行中", "已完成") else "未开始"
        project = db.get(Project, project_id)
        recalc_progress(db, project)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/phases/add")
def phase_add(project_id: int, request: Request, name: str = Form(""),
              db: Session = Depends(get_db), user: User = Depends(login_required)):
    """项目详情：自定义新增施工阶段。"""
    project = db.get(Project, project_id)
    name = name.strip()
    if project and name:
        if not any(p.name == name for p in project.phases):
            max_order = max((p.sort_order for p in project.phases), default=0)
            db.add(ProjectPhase(project_id=project_id, name=name,
                                sort_order=max_order + 1, status="未开始"))
            db.commit()
            recalc_progress(db, project)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.post("/phases/{project_id}/reorder")
async def project_phase_reorder(project_id: int, request: Request,
                               db: Session = Depends(get_db),
                               user: User = Depends(login_required)):
    """项目详情：拖拽排序后保存阶段新顺序。"""
    import json
    try:
        body = await request.body()
        data = json.loads(body or b"{}")
    except Exception:
        data = {}
    ids = data.get("ids", [])
    phases = {p.id: p for p in db.query(ProjectPhase).filter(
        ProjectPhase.project_id == project_id)}
    for idx, pid in enumerate(ids):
        if pid in phases:
            phases[pid].sort_order = idx + 1
    db.commit()
    project = db.get(Project, project_id)
    if project:
        recalc_progress(db, project)
    return JSONResponse({"ok": True})


@router.post("/phases/{phase_id}/move")
def project_phase_move(phase_id: int, request: Request,
                       direction: str = Form("up"),
                       db: Session = Depends(get_db),
                       user: User = Depends(login_required)):
    """项目详情：上移/下移施工阶段顺序。"""
    ph = db.get(ProjectPhase, phase_id)
    if ph is None:
        return RedirectResponse(url="/projects", status_code=303)
    pid = ph.project_id
    phases = db.query(ProjectPhase).filter(
        ProjectPhase.project_id == pid).order_by(ProjectPhase.sort_order).all()
    idx = phases.index(ph)
    if direction == "up" and idx > 0:
        other = phases[idx - 1]
    elif direction == "down" and idx < len(phases) - 1:
        other = phases[idx + 1]
    else:
        return RedirectResponse(url=f"/projects/{pid}", status_code=303)
    ph.sort_order, other.sort_order = other.sort_order, ph.sort_order
    db.commit()
    project = db.get(Project, pid)
    if project:
        recalc_progress(db, project)
    return RedirectResponse(url=f"/projects/{pid}", status_code=303)


@router.post("/phases/{phase_id}/delete")
def project_phase_delete(phase_id: int, request: Request,
                         db: Session = Depends(get_db),
                         user: User = Depends(login_required)):
    """项目详情：删除施工阶段。"""
    ph = db.get(ProjectPhase, phase_id)
    pid = ph.project_id if ph else 0
    if ph:
        db.delete(ph)
        db.commit()
        project = db.get(Project, pid)
        if project:
            recalc_progress(db, project)
    return RedirectResponse(url=f"/projects/{pid}", status_code=303)




def _new_ct_path() -> str:
    import os as _os
    from ..config import DATA_DIR as _DATA_DIR
    return _os.path.join(_DATA_DIR, "new_contract_parsed.json")


def _company_name() -> str:
    """本方（乙方）公司名：取不到就返回空串，不让上传流程因此失败。"""
    try:
        from ..config import DATA_DIR as _DATA_DIR
        with open(os.path.join(_DATA_DIR, "company.json"), encoding="utf-8") as f:
            co = json.load(f)
        return co.get("company_name") or co.get("name", "") or ""
    except Exception:
        return ""


def _contract_msg(request: Request, project_id: int, kind: str, text: str) -> RedirectResponse:
    """上传/保存合同的提示走 session，回详情页时一次性取出。"""
    request.session["contract_msg"] = {"kind": kind, "text": text}
    return RedirectResponse(url=f"/projects/{project_id}#contract", status_code=303)


# 施工合同字段：识别结果按类型回写。字符串/整数/浮点分开是为了让
# 「识别到 0 或空」一律当作**没识别到**，避免把已有数据清空。
_CT_TEXT_FIELDS = ("contract_no", "name", "party_a", "party_b", "pay_method",
                   "a_contact", "b_contact")
_CT_NUM_FIELDS = ("amount", "tax_rate", "prepay_rate", "warranty_retention_rate")
_CT_INT_FIELDS = ("duration_days", "warranty_months")
_CT_DATE_FIELDS = ("sign_date", "start_date", "end_date")


def _apply_parsed_contract(ct: Contract, parsed: dict) -> None:
    """把识别结果写进合同记录：只覆盖识别到的字段，其余保持原值。

    详情页不再提供逐字段编辑表单（合同信息改为只读展示），这里是唯一的写入口，
    必须做到「扫描件/条款缺失不清空已有数据」——识别不到的字段一律不碰；
    识别到的值则以新上传的文件为准（重新上传即更正）。
    """
    for f in _CT_TEXT_FIELDS:
        v = str(parsed.get(f) or "").strip()
        if v:
            setattr(ct, f, v)
    for f in _CT_NUM_FIELDS:
        v = _f(parsed.get(f) or 0)
        if v:
            setattr(ct, f, v)
    for f in _CT_INT_FIELDS:
        v = int(_f(parsed.get(f) or 0))
        if v:
            setattr(ct, f, v)
    for f in _CT_DATE_FIELDS:
        v = _d(str(parsed.get(f) or ""))
        if v:
            setattr(ct, f, v)


def _gen_payment_plan(db: Session, project: Project, amount: float,
                      prepay_rate: float, pay_terms: list, sign_date) -> None:
    """首次导入合同、且项目还没有收款节点时，按付款条款生成收款计划。

    已有收款记录就绝不生成——不能把用户手工排的节点冲掉。
    """
    if db.query(Payment).filter(Payment.project_id == project.id).first():
        return
    amount = amount or 0
    prepay_rate = prepay_rate or 0
    if not (pay_terms or amount > 0):
        return
    if pay_terms:
        for t in pay_terms:
            amt = round(_f(t.get("amount_wan")) * 10000, 2) if t.get("amount_wan") \
                else round(amount * _f(t.get("rate")) / 100, 2)
            db.add(Payment(project_id=project.id, name=t.get("name", ""),
                           plan_amount=amt, plan_date=_d(str(t.get("pay_date") or "")),
                           paid_amount=0))
    elif 0 < prepay_rate < 100:
        db.add(Payment(project_id=project.id, name="预付款",
                       plan_amount=round(amount * prepay_rate / 100, 2),
                       plan_date=sign_date, paid_amount=0))
        db.add(Payment(project_id=project.id, name="结算款",
                       plan_amount=round(amount * (100 - prepay_rate) / 100, 2),
                       plan_date=None, paid_amount=0))
    elif prepay_rate >= 100:
        db.add(Payment(project_id=project.id, name="预付款", plan_amount=amount,
                       plan_date=sign_date, paid_amount=0))
    else:
        db.add(Payment(project_id=project.id, name="合同款", plan_amount=amount,
                       plan_date=sign_date, paid_amount=0))
    db.commit()


# ---------- 施工合同（上传识别后直接落库） ----------
@router.post("/{project_id}/contract/parse")
async def project_contract_parse(project_id: int, request: Request,
                                 file: UploadFile | None = File(None),
                                 db: Session = Depends(get_db),
                                 user: User = Depends(login_required)):
    """上传施工合同文件 → 识别 → **直接写入合同记录**（详情页不再逐字段填表）。"""
    project = db.get(Project, project_id)
    if project is None:
        return RedirectResponse(url="/projects", status_code=303)
    if not file or not file.filename:
        return _contract_msg(request, project_id, "error", "请选择合同文件（PDF / Word）")
    data = await file.read()
    file.file.seek(0)
    file_path = await save_upload(file, "contract", allowed={".pdf", ".docx"})
    if not file_path:
        return _contract_msg(request, project_id, "error", "文件保存失败（仅支持 PDF / Word）")
    parsed, parse_err = recognize_contract_file(file.filename, data)
    if not parsed:
        return _contract_msg(request, project_id, "error",
                             (parse_err or "未能识别该合同（可能是扫描件）").rstrip("。")
                             + "；文件已存档，合同信息未改动")
    ct = project.contract
    if ct is None:
        ct = Contract(project_id=project_id)
        db.add(ct)
    _apply_parsed_contract(ct, parsed)
    if not (ct.party_b or "").strip():
        ct.party_b = _company_name()
    pay_terms = [t for t in (parsed.get("pay_terms") or [])
                 if str(t.get("name") or "").strip()]
    if pay_terms:
        ct.pay_terms = json.dumps(pay_terms, ensure_ascii=False)
    ct.file_path = file_path
    db.commit()
    _gen_payment_plan(db, project, ct.amount, ct.prepay_rate, pay_terms, ct.sign_date)
    summary = "｜".join(x for x in [
        f"编号 {ct.contract_no}" if ct.contract_no else "",
        ct.name or "",
        f"甲方 {ct.party_a}" if ct.party_a else "",
        f"乙方 {ct.party_b}" if ct.party_b else "",
        f"金额 ¥{ct.amount:,.2f}" if ct.amount else "",
        f"签订 {ct.sign_date}" if ct.sign_date else "",
        f"开工 {ct.start_date}" if ct.start_date else "",
        f"竣工 {ct.end_date}" if ct.end_date else "",
        f"付款节点 {len(pay_terms)} 笔" if pay_terms else "",
    ] if x)
    return _contract_msg(request, project_id, "success", f"已识别并保存：{summary}")


@router.post("/{project_id}/contract/manual")
def project_contract_manual(project_id: int, request: Request,
                            contract_no: str = Form(""), name: str = Form(""),
                            party_a: str = Form(""), party_b: str = Form(""),
                            amount: str = Form(""), sign_date: str = Form(""),
                            start_date: str = Form(""), end_date: str = Form(""),
                            duration_days: str = Form(""), pay_method: str = Form(""),
                            tax_rate: str = Form(""), prepay_rate: str = Form(""),
                            warranty_months: str = Form(""),
                            warranty_retention_rate: str = Form(""),
                            pt_name: list[str] = Form([]),
                            pt_rate: list[str] = Form([]),
                            pt_amount: list[str] = Form([]),
                            pt_date: list[str] = Form([]),
                            pt_condition: list[str] = Form([]),
                            db: Session = Depends(get_db),
                            user: User = Depends(login_required)):
    """人工补录 / 修正施工合同信息（扫描件识别不出来时走这里）。

    与上传识别**共用同一套语义**：留空 = 不修改，绝不把已有字段清空。
    所以表单参数一律声明成 `str` 再自己转换——声明成 float/int 的话
    FastAPI 会把空串直接变成 0，「没填」和「填 0」就再也分不开，
    而这正是「保存一次就把合同清零」的来源。
    代价：无法把某个数值字段改成 0（留空即不修改），按当前用法可接受。
    """
    project = db.get(Project, project_id)
    if project is None:
        return RedirectResponse(url="/projects", status_code=303)
    fields = {
        "contract_no": contract_no, "name": name, "party_a": party_a, "party_b": party_b,
        "amount": amount, "sign_date": sign_date, "start_date": start_date,
        "end_date": end_date, "duration_days": duration_days, "pay_method": pay_method,
        "tax_rate": tax_rate, "prepay_rate": prepay_rate,
        "warranty_months": warranty_months, "warranty_retention_rate": warranty_retention_rate,
    }
    ct = project.contract
    if ct is None:
        if not any(str(v).strip() for v in fields.values()):
            return _contract_msg(request, project_id, "error", "没有填写任何字段，合同未创建")
        ct = Contract(project_id=project_id)
        db.add(ct)
    _apply_parsed_contract(ct, fields)
    if not (ct.party_b or "").strip():
        ct.party_b = _company_name()
    pay_terms = []
    for i, pn in enumerate(pt_name):
        pn = (pn or "").strip()
        if pn:
            pay_terms.append({
                "name": pn,
                "rate": _f(pt_rate[i]) if i < len(pt_rate) else 0,
                "amount_wan": _f(pt_amount[i]) if i < len(pt_amount) else 0,
                "pay_date": (pt_date[i] or "").strip() if i < len(pt_date) else "",
                "condition": (pt_condition[i] or "").strip() if i < len(pt_condition) else "",
            })
    if pay_terms:                      # 一行都没填 = 保持原付款节点不变
        ct.pay_terms = json.dumps(pay_terms, ensure_ascii=False)
    db.commit()
    _gen_payment_plan(db, project, ct.amount, ct.prepay_rate, pay_terms, ct.sign_date)
    summary = "｜".join(x for x in [
        f"编号 {ct.contract_no}" if ct.contract_no else "",
        ct.name or "",
        f"甲方 {ct.party_a}" if ct.party_a else "",
        f"乙方 {ct.party_b}" if ct.party_b else "",
        f"金额 ¥{ct.amount:,.2f}" if ct.amount else "",
        f"签订 {ct.sign_date}" if ct.sign_date else "",
        f"开工 {ct.start_date}" if ct.start_date else "",
        f"竣工 {ct.end_date}" if ct.end_date else "",
        f"付款节点 {len(pay_terms)} 笔" if pay_terms else "",
    ] if x)
    return _contract_msg(request, project_id, "success", f"合同信息已保存：{summary}")


@router.post("/{project_id}/contract/note")
def project_contract_note(project_id: int, request: Request,
                          note: str = Form(""),
                          db: Session = Depends(get_db),
                          user: User = Depends(login_required)):
    """只保存合同备注——其余字段在详情页是只读的，改动靠重新上传合同。"""
    project = db.get(Project, project_id)
    if project is None:
        return RedirectResponse(url="/projects", status_code=303)
    if project.contract is None:
        return _contract_msg(request, project_id, "error", "还没有合同记录，请先上传合同")
    project.contract.note = note
    db.commit()
    return _contract_msg(request, project_id, "success", "合同备注已保存")


@router.post("/{project_id}/contract/delete")
def project_contract_delete(project_id: int, request: Request,
                            db: Session = Depends(get_db),
                            user: User = Depends(login_required)):
    project = db.get(Project, project_id)
    if project and project.contract:
        db.delete(project.contract)
        db.commit()
    return RedirectResponse(url=f"/projects/{project_id}#contract", status_code=303)


# ---------- 施工笔记 ----------
@router.post("/{project_id}/notes")
def note_create(project_id: int, request: Request,
                content: str = Form(...), note_date: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    content = content.strip()
    if content:
        db.add(ProjectNote(project_id=project_id, content=content,
                           note_date=_date(note_date) or date.today()))
        db.commit()
    return RedirectResponse(url=f"/projects/{project_id}#notes", status_code=303)


@router.post("/notes/upload-image")
async def note_upload_image(request: Request, file: UploadFile = File(...),
                            db: Session = Depends(get_db),
                            user: User = Depends(login_required)):
    """富文本笔记：上传图片，返回可插入的 URL。"""
    path = await save_upload(file, "notes", allowed=IMAGE_EXTS)
    return JSONResponse({"url": path or ""})


@router.post("/notes/upload-audio")
async def note_upload_audio(request: Request, file: UploadFile = File(...),
                            db: Session = Depends(get_db),
                            user: User = Depends(login_required)):
    """富文本笔记：上传语音，返回可插入的 URL。"""
    path = await save_upload(file, "notes", allowed={".mp3", ".wav", ".ogg", ".m4a", ".webm"})
    return JSONResponse({"url": path or ""})


@router.post("/notes/{note_id}/delete")
def note_delete(note_id: int, request: Request,
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    note = db.get(ProjectNote, note_id)
    pid = note.project_id if note else 0
    if note:
        db.delete(note)
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#notes", status_code=303)


# ---------- 收款 ----------
@router.post("/{project_id}/payments")
def payment_create(project_id: int, request: Request,
                   name: str = Form(""), plan_amount: float = Form(0), plan_date: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    db.add(Payment(project_id=project_id, name=name.strip() or "收款记录",
                   plan_amount=plan_amount,
                   plan_date=_date(plan_date), paid_amount=0))
    db.commit()
    return RedirectResponse(url=f"/projects/{project_id}#payments", status_code=303)


@router.post("/payments/recognize")
async def payment_recognize(request: Request, file: UploadFile = File(...),
                           db: Session = Depends(get_db), user: User = Depends(login_required)):
    """识别银行转账单（甲方回单），返回付款人/收款人/账号/开户行/金额/日期/摘要。"""
    try:
        data = await file.read()
        parsed, parse_err = recognize_bank_file(file.filename or "receipt", data)
        if not parsed:
            return JSONResponse({"ok": False,
                                 "error": parse_err or "未能识别该转账单，请手动填写"})
        _d = parsed.get("trade_date")
        return JSONResponse({"ok": True, "data": {
            "payer": parsed.get("payer", ""), "payee": parsed.get("payee", ""),
            "payer_account": parsed.get("payer_account", ""),
            "payee_account": parsed.get("payee_account", ""),
            "payer_bank": parsed.get("payer_bank", ""),
            "payee_bank": parsed.get("payee_bank", "") or parsed.get("bank", ""),
            "amount": parsed.get("amount", 0),
            "trade_date": str(_d) if _d else "",
            "summary": parsed.get("summary", ""),
        }})
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"识别失败：{e}"})


@router.post("/payments/{payment_id}/pay")
async def payment_pay(payment_id: int, request: Request, amount: float = Form(0),
                      paid_date: str = Form(""), note: str = Form(""),
                      payer: str = Form(""), payee: str = Form(""),
                      payer_account: str = Form(""), payee_account: str = Form(""),
                      payer_bank: str = Form(""), payee_bank: str = Form(""),
                      purpose: str = Form(""),
                      file: UploadFile | None = File(None),
                      db: Session = Depends(get_db), user: User = Depends(login_required)):
    """登记一笔收款（可附甲方转账单凭证，上传后可自动识别金额/日期/收付款方等）。"""
    payment = db.get(Payment, payment_id)
    if payment:
        actual = min(payment.balance, amount)
        file_path = ""
        # 上传甲方转账单存档
        if file and file.filename:
            file_path = await save_upload(file, "receipt", allowed=IMAGE_EXTS | {".pdf"})
        # 金额为空时尝试从转账单识别
        if actual <= 0 and file_path:
            try:
                rel = file_path.replace("/uploads/", "")
                with open(os.path.join(UPLOAD_DIR, rel), "rb") as f:
                    parsed, _recognize_err = recognize_bank_file(file.filename or "receipt", f.read())
                if parsed and parsed.get("amount"):
                    actual = min(payment.balance, parsed["amount"])
                    if not paid_date and parsed.get("trade_date"):
                        paid_date = str(parsed["trade_date"])
                    if not payer:
                        payer = parsed.get("payer", "")
                    if not payee:
                        payee = parsed.get("payee", "")
                    if not payer_account:
                        payer_account = parsed.get("payer_account", "")
                    if not payee_account:
                        payee_account = parsed.get("payee_account", "")
                    if not payer_bank and parsed.get("payer_bank"):
                        payer_bank = parsed["payer_bank"]
                    if not payee_bank and (parsed.get("payee_bank") or parsed.get("bank")):
                        payee_bank = parsed.get("payee_bank") or parsed["bank"]
                    if not purpose:
                        purpose = parsed.get("summary", "")
            except Exception:
                pass
        if actual > 0:
            pay_date = _date(paid_date) or date.today()
            db.add(PaymentReceipt(payment_id=payment_id, amount=actual,
                                  receipt_date=pay_date, note=note, file_path=file_path or "",
                                  payer=payer.strip(), payee=payee.strip(),
                                  payer_account=payer_account.strip(), payee_account=payee_account.strip(),
                                  payer_bank=payer_bank.strip(), payee_bank=payee_bank.strip(),
                                  purpose=purpose.strip()))
            payment.paid_amount = min(payment.plan_amount, payment.paid_amount + actual)
            payment.paid_date = pay_date
            db.commit()
    return RedirectResponse(url=f"/projects/{payment.project_id if payment else 0}#payments", status_code=303)


@router.post("/payments/receipts/{receipt_id}/void")
def receipt_void(receipt_id: int, request: Request, reason: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    """作废一笔收款明细。

    为什么不是删除：file_path 是甲方转账单，是法律凭据。删记录等于把
    凭据一起销毁——而客户争议时「钱收过没有」恰恰要靠它说话。

    已收金额按**增量**回退（与原删除逻辑同口径），不改成「按明细重算」：
    历史数据里有大量 paid_amount 是手工填的、没有对应明细（seed 就是这么
    造的），一旦改成从 receipts 求和，那些手工金额会全部归零。
    """
    receipt = db.get(PaymentReceipt, receipt_id)
    proj_id = 0
    if receipt is not None and receipt.status != VOIDED:
        payment = db.get(Payment, receipt.payment_id)
        if payment:
            proj_id = payment.project_id
            payment.paid_amount = max(0, payment.paid_amount - (receipt.amount or 0))
        mark_voided(receipt, reason)
        db.commit()
        log_audit(db, user, "作废", "收款明细", receipt_id,
                  f"¥{receipt.amount:,.2f}"
                  + (f"｜原因：{reason.strip()}" if reason and reason.strip() else ""))
    return RedirectResponse(url=f"/projects/{proj_id}#payments", status_code=303)


@router.post("/payments/receipts/{receipt_id}/restore")
def receipt_restore(receipt_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    """撤销作废：明细回到已收口径。"""
    receipt = db.get(PaymentReceipt, receipt_id)
    proj_id = 0
    if receipt is not None and receipt.status == VOIDED:
        payment = db.get(Payment, receipt.payment_id)
        if payment:
            proj_id = payment.project_id
            restore(receipt)
            # 恢复时同样按增量加；用 min(plan, ...) 与新增/编辑保持同口径，
            # 免得「新增会截断、恢复不截断」造成两者结果不一致。
            payment.paid_amount = min(payment.plan_amount,
                                      payment.paid_amount + (receipt.amount or 0))
        db.commit()
        log_audit(db, user, "恢复", "收款明细", receipt_id, f"¥{receipt.amount:,.2f}")
    return RedirectResponse(url=f"/projects/{proj_id}#payments", status_code=303)


@router.post("/payments/receipts/{receipt_id}/edit")
def receipt_update(receipt_id: int, request: Request, amount: float = Form(0),
                   receipt_date: str = Form(""), note: str = Form(""),
                   payer: str = Form(""), payee: str = Form(""),
                   payer_account: str = Form(""), payee_account: str = Form(""),
                   payer_bank: str = Form(""), payee_bank: str = Form(""),
                   purpose: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    """编辑收款明细（同步更新节点已收金额）。"""
    receipt = db.get(PaymentReceipt, receipt_id)
    proj_id = 0
    if receipt:
        payment = db.get(Payment, receipt.payment_id)
        if payment:
            proj_id = payment.project_id
            payment.paid_amount = max(0, payment.paid_amount - receipt.amount)
            receipt.amount = round(amount or 0, 2)
            receipt.receipt_date = _date(receipt_date)
            receipt.note = note.strip()
            receipt.payer = payer.strip()
            receipt.payee = payee.strip()
            receipt.payer_account = payer_account.strip()
            receipt.payee_account = payee_account.strip()
            receipt.payer_bank = payer_bank.strip()
            receipt.payee_bank = payee_bank.strip()
            receipt.purpose = purpose.strip()
            payment.paid_amount = min(payment.plan_amount, payment.paid_amount + receipt.amount)
        db.commit()
    return RedirectResponse(url=f"/projects/{proj_id}#payments", status_code=303)


@router.post("/payments/{payment_id}/void")
def payment_void(payment_id: int, request: Request, reason: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    """作废一个收款节点。

    原来是物理删除，而 `Payment.receipts` 带 cascade="all, delete-orphan"，
    删节点会**连带删掉它下面所有收款明细和已上传的转账单**。
    作废后：节点与凭证全部保留、可审计、可恢复，只是不进已收与回款比例。
    """
    payment = db.get(Payment, payment_id)
    pid = payment.project_id if payment else 0
    if payment is not None and payment.void_status != VOIDED:
        _n = len(payment.receipts or [])
        _live = sum(1 for r in (payment.receipts or []) if r.status != VOIDED)
        mark_voided(payment, reason, VOID_ATTRS["payments"])
        db.commit()
        if _live:
            # 节点作废但明细还活着：这是个容易漏的组合——界面上明细还在，
            # 看起来「还能继续收」，实际上这笔账已经不作数了。明确提示，
            # 否则用户会在作废节点下继续登记收款，金额越滚越乱。
            log_audit(db, user, "提示", "收款节点", payment_id,
                      f"该节点已作废，但仍有 {_live} 条未作废的收款明细，"
                      "建议一并作废或先恢复节点")
        log_audit(db, user, "作废", "收款节点", payment_id,
                  f"{payment.name} 计划 ¥{payment.plan_amount:,.2f}"
                  f"（含 {_n} 条收款明细，均已保留）"
                  + (f"｜原因：{reason.strip()}" if reason and reason.strip() else ""))
    return RedirectResponse(url=f"/projects/{pid}#payments", status_code=303)


@router.post("/payments/{payment_id}/restore")
def payment_restore(payment_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    """撤销作废：节点重新计入已收与回款比例。"""
    payment = db.get(Payment, payment_id)
    pid = payment.project_id if payment else 0
    if payment is not None and payment.void_status == VOIDED:
        restore(payment, VOID_ATTRS["payments"])
        db.commit()
        log_audit(db, user, "恢复", "收款节点", payment_id, payment.name)
    return RedirectResponse(url=f"/projects/{pid}#payments", status_code=303)


@router.post("/payments/reorder")
async def payments_reorder(request: Request,
                          db: Session = Depends(get_db),
                          user: User = Depends(login_required)):
    """项目详情：拖拽排序后保存收款节点新顺序。"""
    import json
    try:
        body = await request.body()
        data = json.loads(body or b"{}")
    except Exception:
        data = {}
    ids = data.get("ids", [])
    pays = {int(p.id): p for p in db.query(Payment).all()}
    for idx, pid in enumerate(ids):
        p = pays.get(int(pid))
        if p:
            p.sort_order = idx + 1
    db.commit()
    return {"ok": True}


@router.post("/payments/{payment_id}/edit")
def payment_edit(payment_id: int, request: Request,
                 name: str = Form(""), plan_amount: float = Form(0),
                 plan_date: str = Form(""), note: str = Form(""),
                 db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    """编辑收款节点（名称 / 计划金额 / 计划日期 / 备注）。"""
    p = db.get(Payment, payment_id)
    if p is None:
        return RedirectResponse(url="/projects", status_code=303)
    pid = p.project_id
    if name.strip():
        p.name = name.strip()
    p.plan_amount = plan_amount
    p.plan_date = _date(plan_date)
    p.note = note
    db.commit()
    log_audit(db, user, "修改", "收款节点", p.id, f"编辑节点 {p.name}")
    return RedirectResponse(url=f"/projects/{pid}#payments", status_code=303)


@router.post("/payments/{payment_id}/move")
def payment_move(payment_id: int, request: Request, dir: str = Form("up"),
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    """调整收款节点顺序：与相邻节点交换 sort_order。"""
    p = db.get(Payment, payment_id)
    if p is None:
        return RedirectResponse(url="/projects", status_code=303)
    pid = p.project_id
    nodes = db.query(Payment).filter(Payment.project_id == pid) \
        .order_by(Payment.sort_order, Payment.id).all()
    idx = nodes.index(p) if p in nodes else -1
    n = len(nodes)
    if dir == "up" and idx > 0:
        a, b = nodes[idx - 1], nodes[idx]
        a.sort_order, b.sort_order = b.sort_order, a.sort_order
        db.commit()
    elif dir == "down" and 0 <= idx < n - 1:
        a, b = nodes[idx], nodes[idx + 1]
        a.sort_order, b.sort_order = b.sort_order, a.sort_order
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#payments", status_code=303)


# ---------- 预算 ----------
@router.get("/budgets/template")
def budget_template(request: Request, user: User = Depends(login_required)):
    """下载预算导入模板。"""
    return export_xlsx(
        "预算导入模板.xlsx",
        ["科目", "预算项", "金额"],
        [["主材", "瓷砖/地板/洁具", 32000],
         ["辅材", "水泥沙水电管材", 15000],
         ["人工", "各工种人工费", 35000]],
        "预算表",
        col_widths={1: 12, 2: 26, 3: 12},
    )


@router.post("/{project_id}/budgets/import")
async def budget_import(project_id: int, request: Request, file: UploadFile = File(...),
                        db: Session = Depends(get_db), user: User = Depends(login_required)):
    """从 Excel 导入预算科目。"""
    project = db.get(Project, project_id)
    if project is None:
        return RedirectResponse(url="/projects", status_code=303)
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        msg = "请上传 .xlsx 格式的 Excel 文件"
        return RedirectResponse(
            url=f"/projects/{project_id}?import_msg={urllib.parse.quote(msg)}#costs", status_code=303)
    try:
        data = await file.read()
        rows = parse_budget_excel(data)
    except ValueError as e:
        return RedirectResponse(
            url=f"/projects/{project_id}?import_msg={urllib.parse.quote(str(e))}#costs", status_code=303)

    imported = 0
    for r in rows:
        db.add(BudgetItem(project_id=project_id, category=r["category"],
                          item_name=r["item_name"], budget_amount=r["amount"]))
        imported += 1
    db.commit()
    msg = f"导入成功：共 {imported} 项预算"
    return RedirectResponse(
        url=f"/projects/{project_id}?import_msg={urllib.parse.quote(msg)}#costs", status_code=303)


@router.post("/{project_id}/budgets")
def budget_create(project_id: int, request: Request,
                  category: str = Form("其他"), item_name: str = Form(...),
                  budget_amount: float = Form(0),
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    db.add(BudgetItem(project_id=project_id, category=category, item_name=item_name,
                      budget_amount=budget_amount))
    db.commit()
    return RedirectResponse(url=f"/projects/{project_id}#costs", status_code=303)


@router.post("/budgets/{budget_id}/edit")
def budget_update(budget_id: int, request: Request,
                  category: str = Form("其他"), item_name: str = Form(...),
                  budget_amount: float = Form(0),
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    item = db.get(BudgetItem, budget_id)
    pid = item.project_id if item else 0
    if item:
        item.category = category
        item.item_name = item_name
        item.budget_amount = budget_amount
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#costs", status_code=303)


@router.post("/budgets/{budget_id}/delete")
def budget_delete(budget_id: int, request: Request,
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    item = db.get(BudgetItem, budget_id)
    pid = item.project_id if item else 0
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#costs", status_code=303)


# ---------- 成本 ----------
@router.post("/{project_id}/costs")
async def cost_create(project_id: int, request: Request,
                      category: str = Form("其他费用"), item_name: str = Form(...),
                      amount: float = Form(0), occurred_date: str = Form(""), note: str = Form(""),
                      file: UploadFile | None = File(None),
                      db: Session = Depends(get_db), user: User = Depends(login_required)):
    file_path = await save_upload(file, "expense", allowed=DOC_EXTS | IMAGE_EXTS)
    db.add(CostItem(project_id=project_id, category=category, item_name=item_name,
                    amount=amount, occurred_date=_date(occurred_date), note=note,
                    file_path=file_path or ""))
    db.commit()
    return RedirectResponse(url=f"/projects/{project_id}#costs", status_code=303)


@router.post("/costs/{cost_id}/delete")
def cost_delete(cost_id: int, request: Request,
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    item = db.get(CostItem, cost_id)
    pid = item.project_id if item else 0
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#costs", status_code=303)


# ---------- 采购询价单导出 ----------
@router.post("/{project_id}/inquiry-export")
def inquiry_export(project_id: int, request: Request,
                   pm_ids: list[int] = Form([]), supplier_name: str = Form(""),
                   supplier_id: int = Form(0),
                   qty_mode: str = Form("plan"),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    """勾选项目材料导出采购询价单（xlsx，含供应商报价空列）。"""
    if not supplier_id and supplier_name:
        _sn = supplier_name.strip()
        for _s in db.query(Supplier).all():
            if _sn == _s.name or (_s.name and (_sn in _s.name or _s.name in _sn)):
                supplier_id = _s.id
                break
    project = db.get(Project, project_id)
    if project is None:
        return RedirectResponse(url="/projects", status_code=303)
    if not pm_ids:
        return RedirectResponse(url=f"/projects/{project_id}#materials", status_code=303)
    supplier = db.get(Supplier, supplier_id) if supplier_id else None

    items = []
    rows = db.query(ProjectMaterial).filter(
        ProjectMaterial.id.in_(pm_ids),
        ProjectMaterial.project_id == project_id).all()
    for pm in rows:
        qty = pm.plan_quantity if qty_mode == "plan" else pm.gap
        ref_price = pm.unit_price or pm.material.ref_price
        items.append([len(items) + 1, pm.material.name,
                      pm.material.spec or "", pm.material.unit, qty,
                      ref_price, "", pm.note or ""])
    if not items:
        return RedirectResponse(url=f"/projects/{project_id}#materials", status_code=303)

    today = date.today()
    contact = project.foreman or project.designer or ""
    data = items + [
        [],
        ["询价日期", str(today), "", "", "", "", "", ""],
        ["询价对象", supplier.name if supplier else "（未指定）", "", "", "", "", "", ""],
        ["说明", "请于 5 日内回复含税报价，填写「供应商报价」列并注明交货期。", "", "", "", "", "", ""],
        ["联系人", contact, "", "", "", "", "", ""],
    ]
    return export_xlsx(
        f"采购询价单-{project.name}.xlsx",
        ["序号", "材料名称", "规格型号", "单位", "数量",
         "参考单价(元)", "供应商报价(元)", "备注"],
        data, "采购询价单",
        col_widths={2: 24, 3: 22, 5: 14, 6: 16},
    )


# ---------- 项目材料 ----------
@router.post("/{project_id}/materials")
def project_material_create(project_id: int, request: Request,
                            material_id: int = Form(...), plan_quantity: float = Form(0),
                            purchased_quantity: float = Form(0), unit_price: float = Form(0),
                            db: Session = Depends(get_db), user: User = Depends(login_required)):
    existing = db.query(ProjectMaterial).filter_by(project_id=project_id, material_id=material_id).first()
    if existing:
        existing.plan_quantity += plan_quantity
        existing.purchased_quantity += purchased_quantity
        if unit_price:
            existing.unit_price = unit_price
    else:
        db.add(ProjectMaterial(project_id=project_id, material_id=material_id,
                               plan_quantity=plan_quantity, purchased_quantity=purchased_quantity,
                               unit_price=unit_price))
    db.commit()
    return RedirectResponse(url=f"/projects/{project_id}#materials", status_code=303)


@router.post("/project-materials/{pm_id}/delete")
def project_material_delete(pm_id: int, request: Request,
                            db: Session = Depends(get_db), user: User = Depends(login_required)):
    item = db.get(ProjectMaterial, pm_id)
    pid = item.project_id if item else 0
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#materials", status_code=303)


# ---------- 任务 ----------
@router.post("/{project_id}/tasks")
def task_create(project_id: int, request: Request,
                title: str = Form(...), phase_name: str = Form(""), assignee: str = Form(""),
                assignee_user_id: int = Form(0),
                due_date: str = Form(""), note: str = Form(""),
                parent_id: int = Form(0),
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    """新增任务（可挂在某个父任务下面当子任务）。

    `assignee_user_id` 与自由文本 `assignee` 并存：前者用于「我的任务」精确筛选，
    后者兼容随手写名字（「张工」）。两者不一致时以文本为准显示。

    子任务**只支持一层**：如果传进来的父任务自己也有 parent，就挂到它的父任务上，
    而不是生成三级。跨项目的 parent_id 一律忽略。
    """
    parent = db.get(Task, parent_id) if parent_id else None
    if parent is not None and parent.project_id != project_id:
        parent = None
    if parent is not None and parent.parent_id:
        parent = db.get(Task, parent.parent_id)
    db.add(Task(project_id=project_id, title=title, phase_name=phase_name, assignee=assignee,
                assignee_user_id=assignee_user_id or None,
                due_date=_date(due_date), status="待办", note=note,
                parent_id=parent.id if parent else None))
    db.commit()
    # 回到「新增任务」原地：added=1 让前端滚到新增卡片并聚焦标题输入；
    # 子任务则带 focus=<父任务 id>，滚到父任务那条而不是整页顶部。
    _q = f"?added=1&focus={parent.id}" if parent else "?added=1"
    return RedirectResponse(url=f"/projects/{project_id}{_q}#tasks", status_code=303)


@router.post("/tasks/{task_id}/status")
def task_status(task_id: int, request: Request, status: str = Form("待办"),
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    task = db.get(Task, task_id)
    if task:
        task.status = status if status in ("待办", "进行中", "已完成") else "待办"
        db.commit()
    return RedirectResponse(url=f"/projects/{task.project_id}#tasks", status_code=303)


@router.post("/tasks/{task_id}/edit")
def task_update(task_id: int, request: Request,
                title: str = Form(...), phase_name: str = Form(""),
                assignee: str = Form(""), assignee_user_id: int = Form(0),
                due_date: str = Form(""),
                status: str = Form("待办"), note: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    """编辑任务。

    `assignee_user_id` 用 `Form(0)` 而非 `Form(None)`：留空提交回来是空串，
    声明成 int 会被 FastAPI 校验拒绝（表单空值 → 0 才是合法 int）。
    0 一律当作「没有指定负责人」。
    """
    task = db.get(Task, task_id)
    pid = task.project_id if task else 0
    if task:
        task.title = title
        task.phase_name = phase_name
        task.assignee = assignee
        # 选了账号才覆盖外键；只改文本名字不影响绑定关系，
        # 否则「张工」→「张师傅」这种改名会意外把人的任务弄丢。
        task.assignee_user_id = assignee_user_id or None
        task.due_date = _date(due_date)
        task.status = status if status in ("待办", "进行中", "已完成", "已取消") else "待办"
        task.note = note
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#tasks", status_code=303)


@router.post("/tasks/{task_id}/delete")
def task_delete(task_id: int, request: Request,
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    task = db.get(Task, task_id)
    pid = task.project_id if task else 0
    if task:
        db.delete(task)
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#tasks", status_code=303)


# ---------- 现场记录 ----------
@router.post("/{project_id}/records")
async def record_create(project_id: int, request: Request,
                        record_type: str = Form("施工照片"), content: str = Form(...),
                        record_date: str = Form(""), image: UploadFile | None = File(None),
                        db: Session = Depends(get_db), user: User = Depends(login_required)):
    image_path = await save_upload(image, "site", allowed=IMAGE_EXTS)
    if image_path and not content.strip():
        content = "现场照片"
    db.add(SiteRecord(project_id=project_id, record_type=record_type, content=content,
                      record_date=_date(record_date), image_path=image_path or ""))
    db.commit()
    return RedirectResponse(url=f"/projects/{project_id}#records", status_code=303)


@router.post("/records/{record_id}/delete")
@router.post("/records/{record_id}/status")
def record_status(record_id: int, request: Request,
                  status: str = Form(""),
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    """更新问题记录状态（待处理/处理中/已解决）。"""
    rec = db.get(SiteRecord, record_id)
    pid = rec.project_id if rec else 0
    if rec:
        rec.status = status if status in ("待处理", "处理中", "已解决") else rec.status
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#records", status_code=303)


@router.post("/records/{record_id}/followups")
def record_followup(record_id: int, request: Request,
                    content: str = Form(""),
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    """提交问题后续跟进记录。"""
    rec = db.get(SiteRecord, record_id)
    pid = rec.project_id if rec else 0
    content = content.strip()
    if rec and content:
        db.add(SiteRecordFollowup(site_record_id=record_id, content=content))
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#records", status_code=303)


@router.post("/records/followups/{followup_id}/delete")
def record_followup_delete(followup_id: int, request: Request,
                           db: Session = Depends(get_db),
                           user: User = Depends(login_required)):
    fu = db.get(SiteRecordFollowup, followup_id)
    pid = fu.record.project_id if fu and fu.record else 0
    if fu:
        db.delete(fu)
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#records", status_code=303)


def record_delete(record_id: int, request: Request,
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    rec = db.get(SiteRecord, record_id)
    pid = rec.project_id if rec else 0
    if rec:
        db.delete(rec)
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#records", status_code=303)


# ---------- 文档 ----------
@router.post("/{project_id}/documents")
async def document_create(project_id: int, request: Request,
                          doc_type: str = Form("其他"), title: str = Form(""),
                          note: str = Form(""), file: UploadFile | None = File(None),
                          db: Session = Depends(get_db), user: User = Depends(login_required)):
    file_path = await save_upload(file, "doc", allowed=DOC_EXTS)
    if file and file.filename and not title.strip():
        title = file.filename
    db.add(Document(project_id=project_id, doc_type=doc_type, title=title or "未命名文档",
                    file_path=file_path or "", note=note))
    db.commit()
    return RedirectResponse(url=f"/projects/{project_id}#documents", status_code=303)


@router.post("/documents/{doc_id}/delete")
def document_delete(doc_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    doc = db.get(Document, doc_id)
    pid = doc.project_id if doc else 0
    if doc:
        db.delete(doc)
        db.commit()
    return RedirectResponse(url=f"/projects/{pid}#documents", status_code=303)
