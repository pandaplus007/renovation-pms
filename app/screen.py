"""经营大屏的数据聚合（页面 /dashboard/screen）。

定位：给老板投屏看的一屏经营概览。只读、不可操作、站着能看清。
与首页 `/` 的分工：首页是「今天要处理什么」，大屏是「公司现在什么状况」。

## 行级权限

`build()` 一律先取 `row_scope.visible_projects(db, user, Project)`，
之后所有统计都在这个可见集合内做。

**刻意不复用 `reports._profit_data`**：那个函数签名是 `(db)`，内部
`db.query(Project)` 不带范围条件，因此 `/reports/profit` 及其导出对所有有
reports 查看权的岗位（默认含工程部/财务部/采购部/行政部）展示的是全公司口径。
在大屏上复用它，等于把那个泄露口复制到一个新的、投屏给全公司看的地方。

## 口径纪律

金额口径不另起一套。凡是已有唯一来源的一律调用现有函数：

| 口径 | 唯一来源 |
|---|---|
| 行级范围 | `row_scope.visible_projects` |
| 作废过滤 | `voided.not_voided` / `voided.filter_active` |
| 未审批不入账 | `approvals.filter_booked` |
| 变更按业主签字分界 | `changes.settle_amount` / `project_change_totals` |
| 回款额 / 回款比例 | `Project.recv_amount` / `Project.recv_pct`（只读属性） |
| 施工进度 | `Project.progress`（另一回事，模板里必须标明是「施工」） |
| 逾期与异常扫描 | `todo.collect(db, user)` / `todo.summary(db, user)` |

`Payment` 的作废列在 Python 侧叫 `void_status`（不是 `status`，那个已被
`Payment.status` 这个 property 占用），所以一律走 `VOID_ATTRS`，见 app/voided.py。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from .approvals import filter_booked
from .changes import settle_amount
from .routers.reports import _cost_basis
from .models import Contract, Payment, Project, PROJECT_STATUS
from .row_scope import user_scope, visible_projects
from .voided import filter_active


def _fmt(v: float) -> str:
    """与模板 money 过滤器同口径的千分位（这里只是为了拼 hint 文案）。"""
    if abs(v - round(v)) < 0.005:
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def _wan(v: float) -> str:
    """与模板 money_wan 过滤器同口径的「万」显示。

    ⚠ 刻意**不复用** web.money_wan，而是照抄它的规则：web.py 那个是给
      Jinja 用的，import 进来会让 app.screen 依赖模板层（Jinja2Templates
      在 web.py 里，screen.py 被 CLI 脚本 import 时会拖进整套模板设施）。
      两处规则必须同步改——web.py 里 money_wan 的注释写了阈值与精度。
      跨进程口径漂移过一次：hint 用元、主数字用万，同一张卡片两种单位。
    """
    if v is None:
        return "0"
    if abs(v) < 10000:
        return _fmt(v)
    return f"{v / 10000:,.1f}万"


def _status_dist(projects: list) -> list[dict]:
    """项目按状态的分布，按 PROJECT_STATUS 原顺序输出。

    顺序用 `PROJECT_STATUS` 而不是按数量排序：老板看的是「待签约还有几个」
    「在建几个」，顺序稳定才不会每次刷新跳来跳去。
    """
    out = []
    for st in PROJECT_STATUS:
        rows = [p for p in projects if p.status == st]
        out.append({
            "status": st,
            "count": len(rows),
            "amount": sum((p.contract.amount if p.contract else 0) or 0
                          for p in rows),
        })
    return out


def _dual_progress(projects: list, limit: int = 7) -> tuple[list, list]:
    """返回 (差距最大的项目列表, 「声称在施工却一步没动」的项目列表)。

    ### 为什么画两条

    装修行业施工进度与回款进度经常反向：预付款多的项目回款 90% 而施工才 30%；
    也有做完了才收尾款的，回款 10% 而施工 100%。所以两条必须并排出现，
    并且分别标明是「施工」还是「回款」，任何一条都不能单独写成「进度」。

    ### 为什么按差距排序，而不是按施工进度排序

    差距大的项目才是需要人看一眼的。施工进度最低的那个项目如果两条都低，
    那只是「还没开始」，不是异常——把它排在最前面等于每天报同一个正常的项目。
    差距相同用项目名兜底，保证刷新顺序稳定（否则数字每跳一次顺序就变）。

    ### recv_pct 为 None 的不参与排序

    `recv_pct` 在没有合同时返回 None 而不是 0（0% 会被读成「一分钱没收到」，
    而没有合同是「还没到收钱的时候」）。这类项目算不出差距，混进来排序
    只会让一个没有可比性的数字占掉前排位置。
    """
    rows = []
    for p in projects:
        recv_pct = p.recv_pct
        build_pct = p.progress or 0
        rows.append({
            "p": p,
            "build": build_pct,
            "recv": recv_pct,
            "gap": None if recv_pct is None else abs(build_pct - recv_pct),
            "never_started": p.phase_never_started,
        })

    scored = sorted(
        [r for r in rows if r["gap"] is not None],
        key=lambda r: (-r["gap"], r["p"].name or ""),
    )
    never = [r for r in rows if r["never_started"]]
    return scored[:limit], never[:5]


def _phases(project: Project) -> list[dict]:
    """单个项目的施工阶段（单项目视图用）。

    阶段状态是「未开始 / 进行中 / 已完成」三个词（见 ProjectPhase.status），
    直接走 `state_badge` 过滤器拿颜色——**不自己写映射**。
    `phase_never_started`（声称在施工却一步没动）由调用方单独提示，
    这里不重复表达。
    """
    rows = sorted(project.phases, key=lambda ph: (ph.sort_order or 0, ph.id))
    done = sum(1 for ph in rows if ph.status == "已完成")
    return {
        "rows": [{"name": ph.name, "status": ph.status} for ph in rows],
        "done": done,
        "total": len(rows),
        "pct": round(done / len(rows) * 100) if rows else None,
    }


def _receivable_buckets(pay_rows: list, today: date) -> dict:
    """应收余额按到期日分桶：逾期 + 未来 4 周。

    口径与 todo / dashboard 一致——**排除作废的收款节点**
    （作废 = 这笔钱不算数，留在「该收」里等于催一笔不存在的款）。
    只统计 `balance > 0` 的节点；没有计划日的节点不参与分桶
    （到期日不明就不该出现在时间轴上，放进「逾期」是编造）。
    """
    overdue = 0.0
    weeks = [0.0] * 4
    for pmt in pay_rows:
        bal = pmt.balance or 0
        if bal <= 0 or not pmt.plan_date:
            continue
        if pmt.plan_date < today:
            overdue += bal
            continue
        idx = (pmt.plan_date - today).days // 7
        if 0 <= idx < 4:
            weeks[idx] += bal
    total = overdue + sum(weeks)
    return {
        "overdue": overdue,
        "weeks": weeks,
        "total": total,
        # 柱高按最大值归一。全为 0 时给 1，避免模板里除零。
        "max": max([overdue] + weeks) or 1.0,
    }


def build(db: Session, user, today: date | None = None,
          project: Project | None = None) -> dict:
    """聚合大屏所需的全部数据。一次查询、一次渲染，不做二次取数。

    `project` 不为 None 时切换成**单项目视图**：所有统计都只算这一个项目。
    该项目**必须已经在可见集合内**（调用方负责校验，见 app/routers/screen.py），
    这里仍然按可见集合取交集，不因为传了 project 就绕过行级权限。
    """
    today = today or date.today()

    # ---------- 行级权限：唯一入口 ----------
    projects = visible_projects(db, user, Project)
    if project is not None:
        # 交集而不是「直接用 project」：即使调用方忘了校验，
        # 越权的项目在这里也会被过滤掉，不会出现在大屏上。
        projects = [p for p in projects if p.id == project.id]
    pids = [p.id for p in projects]
    pid_set = set(pids)

    # ---------- 合同额 ----------
    contracts = {}
    if pids:
        contracts = {c.project_id: c for c in db.query(Contract).all()
                     if c.project_id in pid_set}
    contract_total = sum((c.amount or 0) for c in contracts.values())

    # 本月新签按**签约日**算，不是上传日：合同归档晚不代表这个月签的。
    month_prefix = today.strftime("%Y-%m")
    contract_month = sum(
        (c.amount or 0) for c in contracts.values()
        if c.sign_date and c.sign_date.strftime("%Y-%m") == month_prefix)

    # ---------- 收入 ----------
    # 收入 = 合同额 + 已确认变更。分界线是**业主有没有签字**而不是流程走到哪
    # 一步（见 changes 模块文档），所以调 settle_amount，不自己加一遍。
    project_rows = []
    revenue_total = 0.0
    for p in projects:
        ct = contracts.get(p.id)
        revenue = settle_amount(db, p.id, ct) or 0
        revenue_total += revenue
        project_rows.append((p, revenue))
    project_rows.sort(key=lambda r: (-r[1], r[0].name or ""))
    top_projects = [{"p": p, "revenue": rev} for p, rev in project_rows[:6]]

    # ---------- 已回款 ----------
    # 复用 Project.recv_amount（它内部已排除作废节点），不要自己求和。
    # 两处求和口径迟早会漂，而这里错了是「老板看着现金流不对」却查不出原因。
    received = sum(p.recv_amount for p in projects)
    recv_rate = (received / revenue_total * 100) if revenue_total else None

    # ---------- 应收 ----------
    # 排除作废节点。Payment 的作废列在 Python 侧叫 void_status，见 voided.py。
    pay_rows = (db.query(Payment).filter(
        Payment.project_id.in_(pids),
        getattr(Payment, "void_status") != "作废").all()
        if pids else [])
    receivable = sum(x.balance for x in pay_rows)
    buckets = _receivable_buckets(pay_rows, today)

    # ---------- 成本与毛利 ----------
    # ⚠ 成本**不只是费用表**。装修项目的成本大头在采购单与分包单，
    #   只取 cost_items 会算出「毛利率 99%」这种一眼假的数字
    #   （实测：合同额 2059 万、只算费用得到 14 万成本 → 毛利 2045 万）。
    #   所以这里复用 reports._cost_basis 的三种口径，毛利按「已下单」算，
    #   与 /reports/profit 的主口径一致。
    #
    #   _cost_basis 返回的是按 project_id 聚合的 dict（全量，不过滤），
    #   这里按可见项目集合过滤 —— 复用口径，但**不**复用它的范围行为。
    ordered_pur, ordered_sub, commit_pur, commit_sub = _cost_basis(db)

    def _sum_in(d: dict) -> float:
        return float(sum(v for k, v in d.items() if k in pid_set))

    cost_items_total = 0.0
    budget_total = 0.0
    for p in projects:
        budget_total += sum(b.budget_amount for b in p.budget_items)
        # 与 dashboard.py 同源：filter_booked 先排掉未审批通过的费用，
        # filter_active 再排掉作废的。少任何一层，数字就和项目详情页对不上。
        cost_items_total += sum(c.amount for c in
                                filter_active(filter_booked(db, "cost_item",
                                                            p.cost_items)))

    cost_ordered = (_sum_in(ordered_pur) + _sum_in(ordered_sub)
                    + cost_items_total)
    cost_committed = (_sum_in(commit_pur) + _sum_in(commit_sub)
                      + cost_items_total)
    profit = revenue_total - cost_ordered
    profit_rate = (profit / revenue_total * 100) if revenue_total else None

    # ---------- 项目数 ----------
    building = [p for p in projects if p.status == "施工中"]
    # 即将竣工：15 天内到期且未验收。阈值与 dashboard.py 同源。
    due_soon = [p for p in projects
                if p.end_date and p.status not in ("已验收", "售后")
                and p.end_date <= today + timedelta(days=15)]

    # ---------- 待办 ----------
    from .approvals import pending_for_role
    from .todo import collect, summary
    # 单项目视图要把待办也收窄到该项目，否则面板上会出现
    # 「18 种材料低于预警库存」「12 笔变更等业主签字」这类全公司口径的句子。
    _scope_ids = pids or None
    todo_items = collect(db, user, _scope_ids)     # 传 user = 按范围过滤
    todo_stat = summary(db, user, _scope_ids)

    dual, never = _dual_progress(projects)

    return {
        "today": today,
        "user": user,
        "scope": user_scope(user),
        "vis_count": len(projects),
        "project": project,          # None = 全公司视图
        "all_projects": sorted(visible_projects(db, user, Project),
                               key=lambda p: p.name or ""),
        "phases": (_phases(project) if project is not None else None),

        # 主数字带。顺序即视觉权重，不是在这里排序的。
        "hero": [
            {"key": "revenue", "label": "在手合同额", "value": revenue_total,
             "hint": "含已确认变更"},
            {"key": "received", "label": "已回款", "value": received,
             "hint": ("回款率 %.0f%%" % recv_rate) if recv_rate is not None
             else "无合同，暂无回款率"},
            {"key": "receivable", "label": "应收余额", "value": receivable,
             "hint": "未收部分"},
            # hint 里的金额用 _wan（万）而不是 _fmt（元），与主数字同单位。
            # 单位混用（主数字用万、hint 用元）会让人在两张卡片之间
            # 来回换算，而投屏场景里没人有这个耐心。
            {"key": "cost", "label": "已下单成本", "value": cost_ordered,
             "hint": "已签约 %s · 预算 %s" % (_wan(cost_committed),
                                          _wan(budget_total))},
            {"key": "profit", "label": "毛利", "value": profit,
             "hint": ("毛利率 %.0f%%" % profit_rate) if profit_rate is not None
             else "—"},
        ],

        "kpi": {
            "contract_total": contract_total,
            "contract_month": contract_month,
            "building": len(building),
            "due_soon": len(due_soon),
            "due_soon_projects": due_soon[:5],
            # 客户数按「有可见项目关联」算，否则这个数字本身就在泄露公司规模。
            "customers": len({p.customer_id for p in projects if p.customer_id}),
            "month_prefix": month_prefix,
        },

        "buckets": buckets,
        "dist": _status_dist(projects),
        "dual": dual,
        "never_started": never,
        "top_projects": top_projects,
        "todo_stat": todo_stat,
        # 多给几条（12）而不是刚好 8：模板侧会用 JS 按**容器实际高度**
        # 裁到能整条显示的条数。写死 8 会在矮屏上露出半行（半行看起来
        # ���渲染坏了），而顶部三个灯已经给出了完整条数，所以少显示几条
        # 是安全的，露半行不是。
        "todo_items": todo_items[:12],
        "approvals": len(pending_for_role(db, user)),
    }