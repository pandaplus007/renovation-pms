"""报价单（签单漏斗）：从「报了多少」到「成了多少」的转化统计。

为什么单独做
------------
`customers.status` 里本来就有「已报价」这个状态，但没有承载报价内容的对象：
报了多少价、报的什么方案、最后为什么没签，全靠记忆。结果就是
签单率说不清、丢单原因不明、报价水平无从对比——而这三件事恰恰是
小团队最该知道的（决定接不接活、报多少、砍什么）。

漏斗的定义
----------
    「报出」的口径是「所有非草稿的单子」，即已经给客户看过的：
    报出   status != "草稿"        ← 分母
    成交   status == "已成交"
    洽谈中 status == "洽谈中"
    流失   status == "已流失"
    草稿   不计入漏斗（还没给客户看）

    签单率 = 成交数 / 报出数
    金额签单率 = 成交金额 / 报出金额

为什么「已流失」必须在分母里
--------------------------
流失的单子同样报出去过，只是没谈成。把它排除，分母就只剩还在谈的，
签单率会虚高到没法看：报出 5 单成了 3 单、丢了 2 单，若按 3/5 算是 60%，
按 3/3 算就成了 100%。虚高的数字没法指导决策——「签单率 100%」只会
让人以为该涨价了，实际上有一半的单子在丢。

「洽谈中」同理要计入：它已经报出去了，只是还没定。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import QUOTE_STATUS, Customer, Quotation

# 漏斗分母：所有已报出的单子（草稿还没给客户看，不算）
FUNNEL_COUNTED = ("已报出", "洽谈中", "已成交", "已流失")


def _f(x) -> float:
    return float(x or 0)


def _with_total(qs: list[Quotation]) -> list[Quotation]:
    """给 Query 里的对象补上 total 属性（SQLAlchemy 的 hybrid 不便，
    这里在 Python 侧统一算，保证页面和统计用的是同一个数）。"""
    for q in qs:
        q._total = q.total
    return qs


def list_quotes(db: Session, customer_id: int | None = None,
                status: str = "", q: str = "") -> list[dict]:
    qs = db.query(Quotation)
    if customer_id:
        qs = qs.filter(Quotation.customer_id == customer_id)
    if status:
        qs = qs.filter(Quotation.status == status)
    if q:
        like = f"%{q}%"
        qs = qs.filter((Quotation.title.like(like)) | (Quotation.community.like(like))
                      | (Quotation.quote_no.like(like)))
    qs = qs.order_by(Quotation.quote_date.desc().nullslast(), Quotation.id.desc()).all()

    cust = {c.id: c for c in db.query(Customer).all()}
    out = []
    for x in qs:
        total = x.total
        out.append({
            "obj": x, "id": x.id, "no": x.quote_no, "title": x.title,
            "customer": cust.get(x.customer_id),
            "customer_name": (cust.get(x.customer_id).name if cust.get(x.customer_id) else "—"),
            "community": x.community, "area": x.area,
            "requirement_type": x.requirement_type,
            "unit_price": x.unit_price, "discount": x.discount,
            "total": total, "status": x.status,
            "quote_date": x.quote_date, "valid_until": x.valid_until,
            "lost_reason": x.lost_reason, "project_id": x.project_id,
            "item_count": len(x.items),
            "expired": bool(x.valid_until and x.valid_until < __import__("datetime").date.today()
                            and x.status not in ("已成交", "已流失")),
        })
    return out


def funnel(db: Session) -> dict:
    """签单漏斗统计。"""
    qs = db.query(Quotation).all()
    draft = [x for x in qs if x.status == "草稿"]
    quoted = [x for x in qs if x.status in FUNNEL_COUNTED]
    won = [x for x in qs if x.status == "已成交"]
    lost = [x for x in qs if x.status == "已流失"]
    talking = [x for x in qs if x.status == "洽谈中"]

    # 签单率的分母是「所有报出的单子」，不是「还没丢的」。
    # 详见模块开头关于 FUNNEL_COUNTED 的说明。
    q_amt = sum(_f(x.total) for x in quoted)
    w_amt = sum(_f(x.total) for x in won)
    l_amt = sum(_f(x.total) for x in lost)

    # 流失原因分布：这是漏斗里最有信息量的一块——知道为什么丢才可能改
    reasons: dict[str, dict] = {}
    for x in lost:
        r = (x.lost_reason or "").strip() or "未填写"
        d = reasons.setdefault(r, {"reason": r, "count": 0, "amount": 0.0})
        d["count"] += 1
        d["amount"] = round(d["amount"] + _f(x.total), 2)
    reason_rows = sorted(reasons.values(), key=lambda d: -d["count"])

    # 已报出但未定的：金额最大的几单，最该盯
    pending = sorted([x for x in quoted if x.status in ("已报出", "洽谈中")],
                     key=lambda x: -_f(x.total))[:8]
    return {
        "draft": len(draft),
        "quoted_count": len(quoted),
        "won_count": len(won),
        "lost_count": len(lost),
        "talking_count": len(talking),
        "quoted_amount": round(q_amt, 2),
        "won_amount": round(w_amt, 2),
        "lost_amount": round(l_amt, 2),
        # 金额签单率：报出的钱里最终成了多少
        "win_rate_amt": round(w_amt / q_amt * 100, 1) if q_amt else 0,
        # 笔数签单率
        "win_rate_cnt": round(len(won) / len(quoted) * 100, 1) if quoted else 0,
        "avg_quote": round(q_amt / len(quoted), 2) if quoted else 0,
        "avg_won": round(w_amt / len(won), 2) if won else 0,
        "reasons": reason_rows,
        "pending": [{"obj": x, "total": x.total, "status": x.status,
                     "customer": (x.customer.name if x.customer else "—"),
                     "quote_date": x.quote_date} for x in pending],
    }


def by_customer(db: Session) -> list[dict]:
    """按客户看报价历史：老客户复购、报价转成交率。"""
    qs = db.query(Quotation).all()
    cust = {c.id: c for c in db.query(Customer).all()}
    agg: dict = {}
    for x in qs:
        if x.status == "草稿":
            continue
        d = agg.setdefault(x.customer_id, {
            "customer_id": x.customer_id,
            "customer": cust.get(x.customer_id),
            "name": (cust.get(x.customer_id).name if cust.get(x.customer_id) else "—"),
            "count": 0, "won": 0, "lost": 0, "amount": 0.0, "won_amount": 0.0,
        })
        d["count"] += 1
        d["amount"] = round(d["amount"] + _f(x.total), 2)
        if x.status == "已成交":
            d["won"] += 1
            d["won_amount"] = round(d["won_amount"] + _f(x.total), 2)
        elif x.status == "已流失":
            d["lost"] += 1
    out = list(agg.values())
    for d in out:
        d["rate"] = round(d["won"] / d["count"] * 100, 1) if d["count"] else 0
    out.sort(key=lambda d: (-d["count"], -d["amount"]))
    return out


def unit_price_benchmark(db: Session) -> dict:
    """按需求类型（整装/半包/全包）汇总单价区间，用于判断报价是否离谱。

    注意：这里用 unit_price（综合单价）而不是总额除面积——总额可能含设计费、
    管理费，与别的单不可比；综合单价才是同类之间能对话的口径。
    """
    # 没填需求类型的单子归到「未分类」，不整批丢掉——真实数据里大量快速报价
    # 只填了面积和单价。整批排除会让基准样本少到不可用。
    rows = db.query(
        func.trim(func.coalesce(Quotation.requirement_type, "")),
        func.avg(Quotation.unit_price),
        func.min(Quotation.unit_price),
        func.max(Quotation.unit_price),
        func.count(Quotation.id),
    ).filter(
        # 草稿单还没报出去，拿它做基准会把均价拉偏
        Quotation.status.in_(FUNNEL_COUNTED),
        Quotation.unit_price > 0,
    ).group_by(func.trim(func.coalesce(Quotation.requirement_type, ""))).all()
    out = []
    for rt, avg, lo, hi, cnt in rows:
        out.append({
            "requirement_type": (rt or "").strip() or "未分类",
            "avg": round(_f(avg), 0), "min": round(_f(lo), 0),
            "max": round(_f(hi), 0), "count": int(cnt),
        })
    out.sort(key=lambda x: -x["count"])
    return {"rows": out}


def status_counts(db: Session) -> dict:
    """各状态笔数（用于页面上的筛选标签显示数量）。"""
    rows = db.query(Quotation.status, func.count(Quotation.id)).group_by(Quotation.status).all()
    d = {s: 0 for s in QUOTE_STATUS}
    for st, cnt in rows:
        d[st] = int(cnt)
    return d