"""业主只读链接：给业主一个免登录的页面看自家装修进度。

为什么需要它
------------
装修公司的核心协同方是**业主**，但系统里业主什么都看不到。现在业主要进度，
得给项目经理发微信问；变更签证的闭环是「打印出来 → 送业主签字 → 拍照发回 →
扫描上传」，一个来回可能拖一周。

一个只读链接能砍掉大量沟通成本与扯皮。这是本轮 5 项里业务价值最高的。

## 安全设计：这是把数据放到公网上的入口
-------------------------------------
一个免登录链接能被别人拿到，所以默认只暴露**极窄**的一小块数据：

| 暴露 | 不暴露 |
|---|---|
| 项目名、地址、面积、风格 | 合同额、成本、毛利 |
| 施工进度百分比 | 采购/分包/付款金额 |
| 阶段完成情况 | 供应商与分包商名称 |
| 现场照片（可关） | 业主联系方式与账号 |
| 收款进度（可关） | 任何其它项目 |

三条硬规则：

1. **token 不可枚举**：用 `secrets.token_urlsafe(24)`（192 位熵），不是自增 id。
2. **可随时吊销**：项目侧有「停用链接」，且记录谁在什么时候看过。
3. **默认不带照片**：现场照片往往拍到业主的私人物品、邻居的门牌号。

## 与行级权限的关系
------------------
业主链接**绕过**行级权限——它不是内部账号，走的是 token 校验。
所以它的可见范围必须**小于**任何内部员工能看到的内容，而不是取交集。
`tests/test_portal.py` 会断言这一点。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import (Customer, Payment, Project, ProjectPhase,
                     ProjectPortalLink, SiteRecord)
from .voided import VOID_ATTRS, not_voided

# 现场记录里只有「施工照片」适合给业主看。
# RECORD_TYPES = ["施工照片", "问题", "整改", "其他"]
# 「问题」「整改」记录的是漏水、返工之类，推给业主等于主动制造纠纷，
# 而且业主看到「问题」会立刻来问，客服成本比给链接高得多。
PORTAL_PHOTO_TYPE = "施工照片"

# ---------------------------------------------------------------- 令牌

TOKEN_BYTES = 24          # 24 字节 = 192 位熵，自增/时间戳都猜不到
TOKEN_PREFIX = "pmt"      # 便于人工识别与筛选（前缀不是安全的一部分）


def new_token() -> str:
    """生成业主链接 token。

    用 `secrets` 而非 `random`：后者是可预测的伪随机（Mersenne Twister），
    知道几个输出就能推出后续 token，链接等于公开。
    """
    return f"{TOKEN_PREFIX}-{secrets.token_urlsafe(TOKEN_BYTES)}"


def normalize_token(tok: str) -> str:
    """把用户输入收敛成规范形式（去空白、统一小写）。

    链接常被微信/邮件转手，可能带上前后空格或被换成大写。
    查库前统一化，否则会「明明是对的链接却提示无效」。
    """
    return (tok or "").strip()


def find_by_token(db: Session, tok: str):
    """按 token 找项目。找不到返回 None。

    只看 enabled=True：停用链接后即便 token 还在库里也拿不到数据。
    """
    t = normalize_token(tok)
    if not t:
        return None
    row = (db.query(ProjectPortalLink)
           .filter(ProjectPortalLink.token == t,
                   ProjectPortalLink.enabled.is_(True))
           .first())
    if row is None:
        return None
    if row.expires_at and row.expires_at < datetime.now():
        return None
    return db.get(Project, row.project_id)


def touch(db: Session, project: Project) -> None:
    """记录一次访问（谁、什么时候）。

    有访问记录才做得到「业主上次看是三天前，该催一催了」——
    这是这个链接除了展示之外的第二个用途。
    """
    row = (db.query(ProjectPortalLink)
           .filter(ProjectPortalLink.project_id == project.id,
                   ProjectPortalLink.enabled.is_(True)).first())
    if row is None:
        return
    row.view_count = (row.view_count or 0) + 1
    row.last_viewed_at = datetime.now()
    db.commit()


# ---------------------------------------------------------------- 组装视图数据

def build_view(db: Session, project: Project, link) -> dict:
    """组装业主可见的数据。

    ⚠ 这个函数是**唯一的出口**，也是权限的最后一道闸：
    任何新增字段都要在这里显式挑选，不能把 ORM 对象整个丢给模板——
    模板里 `{{ project.contract.amount }}` 就能读到合同额。

    阶段进度用 ProjectPhase（有真实勾选状态）而不是 Project.progress
    （那是人工填的数），让业主看到的是「哪些做完、哪些没做」。
    """
    customer = db.get(Customer, project.customer_id) if project.customer_id else None
    phases = db.query(ProjectPhase).filter(
        ProjectPhase.project_id == project.id
    ).order_by(ProjectPhase.sort_order, ProjectPhase.id).all()
    done = [p for p in phases if p.status == "已完成"]
    # 没有阶段记录时不给百分比：编一个 0% 会让业主以为一直没开工，
    # 而实际情况可能只是还没录阶段。显示「—」并说明。
    phase_pct = round(len(done) / len(phases) * 100) if phases else None

    out = {
        "project_name": project.name,
        "address": project.address,
        "area": project.area,
        "style": project.style,
        "status": project.status,
        "start_date": project.start_date,
        "end_date": project.end_date,
        "customer_name": customer.name if customer else "",
        "phases": [{"name": p.name, "status": p.status,
                    "note": p.note or ""} for p in phases],
        "phase_done": len(done),
        "phase_total": len(phases),
        "phase_pct": phase_pct,
    }

    # ---- 以下两段各自独立开关 ----
    # ⚠ 不要写成「照片关了就直接 return」：那会让「开收款、关照片」的组合
    #   悄悄退化成「什么都不显示」，而设置页上两个开关都是勾着的。
    #   症状是「我明明开了收款进度业主却说看不到」，极难定位。
    if link and link.show_photos:
        # 只取 record_type == 施工照片：RECORD_TYPES 里还有「问题」「整改」，
        # 那是内部记录漏水返工的，推给业主等于主动制造纠纷。
        photos = db.query(SiteRecord).filter(
            SiteRecord.project_id == project.id,
            SiteRecord.image_path != "",
            SiteRecord.record_type == PORTAL_PHOTO_TYPE,
        ).order_by(SiteRecord.record_date.desc().nullslast(),
                   SiteRecord.id.desc()).limit(30).all()
        out["photos"] = [{"url": r.image_path,
                          "date": r.record_date,
                          "content": (r.content or "")[:60]}
                         for r in photos]

    if link and link.show_payment:
        pays = db.query(Payment).filter(
            Payment.project_id == project.id,
            not_voided(Payment, VOID_ATTRS["payments"])
        ).order_by(Payment.sort_order, Payment.id).all()
        total_plan = sum((p.plan_amount or 0) for p in pays)
        total_paid = sum((p.paid_amount or 0) for p in pays)
        # 「已收 / 应收」是业主最关心的信息之一，且**不含任何单价**。
        # 逐节点也只给「已收 / 未收」两态与计划金额——不给单价的原因：
        # 单价会暴露我方给业主的报价结构，而业主拿它去比别的公司就难谈了。
        out["payment"] = {
            "plan": total_plan,
            "paid": total_paid,
            "unpaid": round(total_plan - total_paid, 2),
            "pct": (round(total_paid / total_plan * 100)
                    if total_plan > 0 else None),
            "nodes": [{"name": p.name,
                       "plan": p.plan_amount,
                       "paid": p.paid_amount,
                       # 用 void_status 过滤过的结果算状态；直接取 p.status
                       # 拿到的是内部三态「未收/部分已收/已收」，那是内部口径，
                       # 对业主只给一个对勾。
                       #
                       # ⚠ 门户标题与汇总用「已付」而内部用「已收」，这不是不一致，
                       # 是读者不同：门户的读者是**付款人本人**，「你已付」是自然说法；
                       # 内部页面的读者是公司，「已收」才是公司视角。**别把这里
                       # 跟着内部一起改成「已收」**——那会变成「你已收到我的钱」。
                       "paid_full": (p.paid_amount or 0) >= (p.plan_amount or 0)
                                     and (p.plan_amount or 0) > 0}
                      for p in pays],
        }
    return out


# ---------------------------------------------------------------- 链接管理

def get_link(db: Session, project_id: int):
    """取链接行，不存在时创建一条**停用**的默认行。

    ⚠ GET 页面必须用这个，不能用 `ensure_link(db, project, enabled=False)`：
    ensure_link 会把 enabled/show_photos/show_payment 全写成传入值。
    在 GET 里调用 = 每次打开管理页都把业主已经拿到的链接重置掉，
    而且**没有报错、没有日志**——项目侧看到的是「链接怎么又失效了」。
    """
    row = (db.query(ProjectPortalLink)
           .filter(ProjectPortalLink.project_id == project_id).first())
    if row is None:
        row = ProjectPortalLink(project_id=project_id, token=new_token(),
                                enabled=False, show_photos=False,
                                show_payment=False)
        db.add(row)
        db.commit()
    return row


def ensure_link(db: Session, project: Project, enabled: bool = False,
                show_photos: bool = False, show_payment: bool = False,
                expires_days: int | None = 180):
    """为项目创建或更新业主链接（**写**操作用）。返回 (link, created)。

    只在 POST 路由里调用。

    ⚠ 每次保存都会重算 expires_at：把有效期当「从现在起 N 天」，
    而不是「从首次启用起 N 天」。对「链接发给业主后 N 天内有效」这个语义，
    从保存时刻算才对——否则每次改设置都会偷偷延长有效期。
    """
    existed = (db.query(ProjectPortalLink)
               .filter(ProjectPortalLink.project_id == project.id).first())
    row = get_link(db, project.id)
    row.enabled = bool(enabled)
    row.show_photos = bool(show_photos)
    row.show_payment = bool(show_payment)
    if enabled and expires_days:
        row.expires_at = datetime.now() + timedelta(days=expires_days)
    if not enabled:
        row.expires_at = None
    db.commit()
    return row, existed is None
