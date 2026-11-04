"""销项开票：向客户开票的申请台账。

现状问题
--------
`invoices` 表里 41 张全是进项，销项 0 张关联项目。这不是「没开过票」，
而是**开了票没往系统里记**。后果：
  1. 收了 1376 万，对外开出去多少票说不清
  2. 开票额无法与收款节点核对，存在超开的风险
  3. 建筑业预收款/进度款的开票有政策要求（预收款需按预付比例预缴），
     没有台账就没法自查

本模块做的事
------------
把「向某个客户开多少税」变成一条有状态的申请单（待开票 → 已开票/部分开票/
已作废/已驳回），并在页面上把三条数并排：

    已收款  vs  已开票  vs  待开票

三者对不上时，一眼能看出开票节奏是快了还是慢了。

金额口径
--------
    amount           申请金额（含税），这是填给客户的数
    invoiced_amount  已开票金额
    remain           = amount − invoiced_amount（待开票）
    tax_excl         = amount ÷ (1 + 税率)   不含税
    tax_amount       = amount − tax_excl     税额
发票录入（`invoices` 表）不在本模块内做——那里已有进项发票的录入流程，
销项发票仍然走它，只是从这里带出 customer/project/payment 关联。
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Invoice, InvoiceApplication, Payment, Project
from .voided import VOID_ATTRS, not_voided


def _f(x) -> float:
    return float(x or 0)


def list_applications(db: Session, status: str = "", project_id: int | None = None,
                      customer_id: int | None = None) -> list[dict]:
    qs = db.query(InvoiceApplication)
    if status:
        qs = qs.filter(InvoiceApplication.status == status)
    if project_id:
        qs = qs.filter(InvoiceApplication.project_id == project_id)
    if customer_id:
        qs = qs.filter(InvoiceApplication.customer_id == customer_id)
    qs = qs.order_by(InvoiceApplication.apply_date.desc().nullslast(),
                     InvoiceApplication.id.desc()).all()

    from .models import Customer
    cust = {c.id: c for c in db.query(Customer).all()}
    proj = {p.id: p for p in db.query(Project).all()}

    out = []
    for a in qs:
        out.append({
            "obj": a, "id": a.id,
            "customer": cust.get(a.customer_id),
            "customer_name": (cust.get(a.customer_id).name if cust.get(a.customer_id) else "—"),
            "project": proj.get(a.project_id),
            "project_name": (proj.get(a.project_id).name if proj.get(a.project_id) else ""),
            "payment": a.payment,
            "amount": _f(a.amount),
            "invoiced": _f(a.invoiced_amount),
            "remain": a.remain,
            "tax_excl": a.tax_excl, "tax_amount": a.tax_amount,
            "tax_rate": a.tax_rate, "invoice_type": a.invoice_type,
            "title": a.title, "tax_no": a.tax_no,
            "status": a.status, "apply_date": a.apply_date, "finish_date": a.finish_date,
            "invoice": a.invoice, "note": a.note,
        })
    return out


def received_vs_invoiced(db: Session) -> dict:
    """已收 vs 已开票 的对照，按项目 + 总计。

    已收款来自 payments.paid_amount（未审批不入账只管付款与费用，
    收款不在阻断范围内，所以这里直接求和）。
    已开票优先取 invoice_applications 的已开票额；历史上有直接录进
    invoices 表的销项发票（status != 作废 且 direction == 销项），
    也要算进来，否则改造前的数据会在对照表里凭空消失。
    """
    received = dict(db.query(Payment.project_id,
                             func.coalesce(func.sum(Payment.paid_amount), 0.0))
                    .filter(not_voided(Payment, VOID_ATTRS["payments"]))   # 作废收款节点不算「已收」
                    .group_by(Payment.project_id).all())

    app_invoiced = dict(db.query(
        InvoiceApplication.project_id,
        func.coalesce(func.sum(InvoiceApplication.invoiced_amount), 0.0),
    ).filter(
        InvoiceApplication.project_id.isnot(None),
        InvoiceApplication.status != "已作废",
    ).group_by(InvoiceApplication.project_id).all())

    app_pending = dict(db.query(
        InvoiceApplication.project_id,
        func.coalesce(func.sum(
            func.max(InvoiceApplication.amount - InvoiceApplication.invoiced_amount, 0.0)), 0.0),
    ).filter(
        InvoiceApplication.project_id.isnot(None),
        InvoiceApplication.status.in_(("待开票", "部分开票")),
    ).group_by(InvoiceApplication.project_id).all())

    # 直接录进 invoices 的销项发票——改造前手工录入的存量数据，不经申请单。
    #
    # 必须排除 app_id 非空的那些：那批是本模块从申请单开出的票，金额已经
    # 通过 app_invoiced 统计过了，两边都算就把同一张票数了两遍。
    # 一度用 invoice_id 反查，但一张申请单可分次开多张票，invoice_id 只记
    # 得住最后一张，前面的会漏判——所以给 Invoice 加了 app_id 冗余列。
    inv_direct = dict(db.query(
        Invoice.project_id,
        func.coalesce(func.sum(Invoice.amount), 0.0),
    ).filter(
        Invoice.direction == "销项",
        Invoice.status != "作废",
        Invoice.project_id.isnot(None),
        Invoice.app_id.is_(None),
    ).group_by(Invoice.project_id).all())

    rows = []
    t_recv = t_inv = t_pending = 0.0
    for p in db.query(Project).order_by(Project.id).all():
        recv = _f(received.get(p.id))
        inv = _f(app_invoiced.get(p.id)) + _f(inv_direct.get(p.id))
        pending = _f(app_pending.get(p.id))
        # 已收但没开票的部分：不是「待开票申请」的未开额，
        # 而是收了钱却连申请单都没建的部分——这才是真正该盯的缺口
        gap = round(recv - inv, 2)
        rows.append({
            "project": p, "received": round(recv, 2), "invoiced": round(inv, 2),
            "pending": round(pending, 2), "gap": gap,
            "gap_rate": round(gap / recv * 100, 1) if recv else 0,
        })
        t_recv += recv
        t_inv += inv
        t_pending += pending

    # 无关联项目的申请单也要提示：钱收了但单子挂在客户上、没挂项目
    orphan = (db.query(InvoiceApplication)
              .filter(InvoiceApplication.project_id.is_(None),
                      InvoiceApplication.status.in_(("待开票", "部分开票")))
              .all())

    return {
        "rows": rows,
        "total_received": round(t_recv, 2),
        "total_invoiced": round(t_inv, 2),
        "total_pending": round(t_pending, 2),
        "total_gap": round(t_recv - t_inv, 2),
        "gap_rate": round((t_recv - t_inv) / t_recv * 100, 1) if t_recv else 0,
        "orphan_count": len(orphan),
        "orphan_amount": round(sum(_f(a.amount) - _f(a.invoiced_amount) for a in orphan), 2),
    }


def status_counts(db: Session) -> dict:
    from .models import INVOICE_APPLICATION_STATUS
    rows = db.query(InvoiceApplication.status, func.count(InvoiceApplication.id)) \
             .group_by(InvoiceApplication.status).all()
    d = {s: 0 for s in INVOICE_APPLICATION_STATUS}
    for st, cnt in rows:
        d[st] = int(cnt)
    return d


def over_invoiced_warnings(db: Session) -> list[dict]:
    """开票额超过已收款的申请单——必须有人签字才能开，否则是虚开。

    这条校验放在列表页而不是提交时，是因为开票常常是「先开票后收款」
    （预收款发票），硬阻断会挡住正常业务。这里只提示，不阻止。
    """
    out = []
    for a in db.query(InvoiceApplication).filter(
            InvoiceApplication.status != "已作废").all():
        recv = 0.0
        if a.payment:
            # 节点自身已作废 → 这笔「已收」不存在
            if getattr(a.payment, "void_status", "正常") != "作废":
                recv = _f(a.payment.paid_amount)
        elif a.project_id:
            recv = _f(db.query(func.coalesce(func.sum(Payment.paid_amount), 0.0))
                      .filter(Payment.project_id == a.project_id,
                              not_voided(Payment, VOID_ATTRS["payments"])).scalar())
        if _f(a.invoiced_amount) > recv + 0.01:
            out.append({
                "obj": a,
                "customer_name": (a.customer.name if a.customer else "—"),
                "invoiced": _f(a.invoiced_amount), "received": round(recv, 2),
                "over": round(_f(a.invoiced_amount) - recv, 2),
                "status": a.status,
            })
    out.sort(key=lambda x: -x["over"])
    return out


def create_invoice_for_app(db: Session, app: InvoiceApplication,
                           invoice_no: str, invoice_date, amount: float,
                           file_path: str = "") -> Invoice:
    """从申请单开出销项发票，并回写已开票金额与状态。

    amount 可以小于申请额（分次开票）。传 0 或负数时按剩余额全额开具——
    「开剩下的」是最常见的操作，不该要求用户先算一遍剩余多少。
    """
    remain = app.remain
    amt = _f(amount) if _f(amount) > 0 else max(remain, 0.0)
    if amt > remain + 0.01 and remain >= 0:
        # 开超了申请额：允许（现实里改过金额的申请单不少），
        # 但由调用方决定是否提示，这里只在数据上如实记录
        pass
    inv = Invoice(
        direction="销项",
        invoice_type=app.invoice_type,
        invoice_no=invoice_no or "",
        amount=amt,
        tax_excl=round(amt / (1 + _f(app.tax_rate) / 100), 2) if _f(app.tax_rate) else 0,
        invoice_date=invoice_date,
        title=app.title or (app.customer.name if app.customer else ""),
        tax_no=app.tax_no or (app.customer.tax_no if app.customer else ""),
        customer_id=app.customer_id,
        project_id=app.project_id,
        payment_id=app.payment_id,
        app_id=app.id,
        file_path=file_path or "",
        status="正常",
        note=f"由开票申请单 #{app.id} 开出",
    )
    db.add(inv)
    db.flush()
    app.invoiced_amount = round(_f(app.invoiced_amount) + amt, 2)
    app.invoice_id = inv.id
    app.finish_date = invoice_date
    remain = app.remain
    if remain <= 0.01:
        app.status = "已开票"
    elif app.invoiced_amount > 0:
        app.status = "部分开票"
    db.commit()
    return inv