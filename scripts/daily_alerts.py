"""每日预警扫描脚本：汇总收款/工期/库存/超付预警，输出中文报告。

供 Proma 定时任务每天调用。用法：python scripts/daily_alerts.py
"""
import os
import sys
from datetime import date, timedelta

# Windows 控制台默认 GBK，强制 UTF-8 输出避免 emoji/中文报错
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func

from app.database import SessionLocal
from app.models import (Material, Payment, Project, PurchaseContract,
                        SubContract, SupplierPayment)


def main() -> str:
    db = SessionLocal()
    today = date.today()
    sections = []

    # 1. 收款预警（计划日已到 / 7 天内到期且未付清）
    recv = []
    for p in db.query(Payment).all():
        if p.plan_date and p.balance > 0 and p.plan_date <= today + timedelta(days=7):
            overdue = "【已逾期】" if p.plan_date < today else ""
            recv.append(f"· {overdue}{p.project.name} · {p.name}：应收 ¥{p.balance:,.0f}（计划 {p.plan_date}）")
    if recv:
        sections.append(f"💰 收款预警（{len(recv)} 项）\n" + "\n".join(recv))

    # 2. 工期预警（15 天内到期或已超期，未验收/售后）
    ddl = []
    for p in db.query(Project).filter(
            Project.end_date.isnot(None),
            Project.status.notin_(["已验收", "售后"])).all():
        if p.end_date and p.end_date <= today + timedelta(days=15):
            overdue = "【已超期】" if p.end_date < today else ""
            ddl.append(f"· {overdue}{p.name}（{p.status}），计划竣工 {p.end_date}")
    if ddl:
        sections.append(f"📅 工期预警（{len(ddl)} 项）\n" + "\n".join(ddl))

    # 3. 库存预警
    stock = []
    for m in db.query(Material).all():
        if m.low_stock_flag:
            stock.append(f"· {m.name} {m.spec or ''}：库存仅剩 {m.stock} {m.unit}（预警线 {m.low_stock}）")
    if stock:
        sections.append(f"📦 库存预警（{len(stock)} 项）\n" + "\n".join(stock))

    # 4. 超付预警（采购合同 + 分包合同）
    over = []
    _pc_paid = dict(db.query(SupplierPayment.contract_id,
                             func.coalesce(func.sum(SupplierPayment.amount), 0.0)).filter(
        SupplierPayment.contract_id.isnot(None)).group_by(SupplierPayment.contract_id).all())
    for pc in db.query(PurchaseContract).all():
        paid = float(_pc_paid.get(pc.id) or 0)
        if pc.amount and paid > pc.amount:
            over.append(f"· 采购合同 {pc.name}（{pc.contract_no or '无编号'}）：已付 ¥{paid:,.0f} > 合同额 ¥{pc.amount:,.0f}，超付 ¥{paid - pc.amount:,.0f}")
    _sc_paid = dict(db.query(SupplierPayment.subcontract_id,
                             func.coalesce(func.sum(SupplierPayment.amount), 0.0)).filter(
        SupplierPayment.subcontract_id.isnot(None)).group_by(SupplierPayment.subcontract_id).all())
    for sc in db.query(SubContract).all():
        paid = float(_sc_paid.get(sc.id) or 0)
        if sc.amount and paid > sc.amount:
            over.append(f"· 分包合同 {sc.name}（{sc.contract_no or '无编号'}）：已付 ¥{paid:,.0f} > 合同额 ¥{sc.amount:,.0f}，超付 ¥{paid - sc.amount:,.0f}")
    if over:
        sections.append(f"⚠️ 超付预警（{len(over)} 项）\n" + "\n".join(over))

    db.close()
    if not sections:
        return "✅ 今日无预警：收款、工期、库存、超付均正常。"
    return "📋 装修项目管理系统 · 每日预警\n" + today.strftime("%Y-%m-%d") + "\n\n" + "\n\n".join(sections)


if __name__ == "__main__":
    print(main())
