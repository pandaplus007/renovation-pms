"""作废（软删除）的统一口径。

为什么这两张表不做物理删除
------------------------
`supplier_payments`（付款）与 `cost_items`（费用）是**直接进台账和利润的钱**。
录错一行就 `db.delete()` 物理删掉，等于账上没有痕迹：审计日志只能证明
「某时某人删过」，恢复不了。对账、税务核查、客户扯皮时这是要出事的。

所以改成「标记作废」：行还在，能查、能导出、能审计、能恢复，
但**不计入任何金额与条数**。

口径只有一条
------------
**作废 = 不进任何 sum，不进任何 count。**

凡是碰这两张表的金额或条数的地方（台账、毛利、应付、供应商对账、超付预警、
成本超预算……），都必须经过本模块过滤。漏一处的后果特别难查：
页面不报错、测试也可能照样绿，只是数字悄悄错了一点。
所以新增统计口径时，请一律用 `active()` 而不是手写 `status != '作废'`——
手写会漏掉 NULL 兜底，见下。

⚠ 为什么要带 is_(None) 兜底
--------------------------
SQL 里 `NULL != '作废'` 的结果是 **NULL（假）**，也就是说
`filter(status != '作废')` 会把 status 为 NULL 的行**全部过滤掉**——
历史数据会在加列后集体消失，而且症状是「列表变空了」，很容易被误判成
迁移失败。database._migrate() 里已回填，但仍然保留兜底：
从旧备份恢复、或手工插库都可能造出 NULL，那时代码会崩在错误的地方。

与「未审批不入账」的关系
------------------------
两者是**并列**的排除条件，不是替代关系：
    入账 = 未作废 **且** 已审批通过（若该类型开了未审批不入账）
`approvals.filter_booked` 管后者，本模块管前者。两个都要过。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_

from .models import VOID_STATUS

ACTIVE = VOID_STATUS[0]   # 正常
VOIDED = VOID_STATUS[1]   # 作废


def not_voided(model, attr: str = "status"):
    """SQL 条件：未作废。用于 `query(...).filter(not_voided(CostItem))`。

    刻意**不叫** `active`：`active` 在这套代码里已经是模板侧栏高亮的变量名
    （suppliers.py 里的 `active = "supplier_payments" if ...`），
    用同名函数会在那个文件里被局部变量直接遮蔽，且不报错——只是悄悄
    把 SQL 条件换成了一个字符串。

    `attr` 的存在是因为 **`Payment` 的作废列在 Python 侧叫 `void_status`**：
    该类已有一个 `status` @property（未收/部分/已收清），同名会让
    `model.status` 解析成 property 而非列，报
    「'property' object has no attribute 'is_'」。
    数据库里的列名仍是 `status`（靠 mapped_column("status", ...) 映射），
    所以 SQL 与迁移都不受影响，只有 Python 属性名不同。

    带 `is_(None)` 兜底，理由见模块注释的 ⚠ 段。
    代价是走不了 status 索引——这几张表都在几千行量级，可接受；
    真到量级瓶颈，正确性优先，届时改用回填 + 纯 `!=` 并加约束。
    """
    col = getattr(model, attr)
    return or_(col != VOIDED, col.is_(None))


# 各类作废列在 Python 侧的属性名。与 VOID_MODELS 同步。
VOID_ATTRS = {
    "supplier_payments": "status",
    "cost_items": "status",
    "payment_receipts": "status",
    "payments": "void_status",   # 与 status @property 撞名，见 models.py 注释
}


def filter_active(rows, attr: str = "status"):
    """内存过滤已取回的 ORM 对象列表。

    与 `not_voided()` 同口径。用于「先查出一批对象再逐条判断」的场景
    （如逐条超付检查），避免再发一次查询。
    """
    return [r for r in (rows or []) if getattr(r, attr, ACTIVE) != VOIDED]


def mark_voided(obj, reason: str = "", attr: str = "status"):
    """把一张单据标记为作废。就地修改对象，不 commit —— 调用方负责事务。

    `attr` 与 not_voided 同一个理由：`Payment` 的作废列在 Python 侧叫
    `void_status`。不传就会撞上那个只读的 `status` @property，报
    「property 'status' has no setter」——同样只在收款路径出现。
    """
    setattr(obj, attr, VOIDED)
    obj.void_reason = (reason or "").strip()[:200]
    obj.voided_at = datetime.now()
    return obj


def restore(obj, attr: str = "status"):
    """撤销作废。理由留痕不清空——它记录的是「曾经作废过」这件事。"""
    setattr(obj, attr, ACTIVE)
    obj.voided_at = None
    return obj
