"""审批流核心逻辑。

设计边界（重要）
----------------
本模块**只做留痕，不改记账**。项目台账/利润报表里，付款与费用自录入那一刻起就已
实时汇总（见 reports 路由）。审批通过不会、也不应该再次写钱——那会造成重复入账。
审批的价值是「这条支出谁批的、什么时候批的、批的理由是什么」可追溯。

因此 `finalize_approval()` 刻意不含任何金额写操作。

状态机
------
    提交 → 待审批 → 审批中 →(逐节点通过)→ 已通过
                              →(任一节点驳回)→ 已驳回 →(可重新提交)→ 审批中
    提交人可主动撤回 → 已撤回 →(可重新提交)→ 审批中

`current_step` 为当前待处理节点下标；已通过时等于 total_steps。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy.orm import Session

from .models import (BLOCK_BOOKING_TYPES, ApprovalFlow, ApprovalRequest,
                     ApprovalStep, AuditLog, CostItem, PurchaseContract,
                     SubContract, SupplierPayment, User, now)
from .permissions import BUILTIN_ROLES

# 需要审批的单据类型 → (中文名, 取标题/金额的函数)
ENTITY_LABELS = {
    "supplier_payment": "供应商付款",
    "purchase_contract": "采购合同",
    "sub_contract": "分包合同",
    "cost_item": "费用报销",
}

# 会把审批标记为"业务单据已删除"的实体
ENTITY_MODELS = {
    "supplier_payment": SupplierPayment,
    "purchase_contract": PurchaseContract,
    "sub_contract": SubContract,
    "cost_item": CostItem,
}


class ApprovalError(Exception):
    """审批操作被拒绝（业务规则不满足）。路由层转成 400 并给出可读信息。"""


# ---------------------------------------------------------------- 流程定义

def get_flow(db: Session, entity_type: str) -> ApprovalFlow | None:
    return db.query(ApprovalFlow).filter(ApprovalFlow.entity_type == entity_type).first()


def flow_steps(flow: ApprovalFlow | None) -> list[str]:
    """解析岗位链。配置为空/损坏时返回空列表，调用方据此自动通过。"""
    if not flow or not flow.steps:
        return []
    try:
        data = json.loads(flow.steps)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x).strip() for x in data if str(x).strip()]


def is_enabled(db: Session, entity_type: str) -> bool:
    """该单据类型是否需要审批。未配置流程 = 不需要审批（保持现状，不打扰日常记账）。"""
    f = get_flow(db, entity_type)
    return bool(f and f.enabled and flow_steps(f))


# ---------------------------------------------------------------- 单据快照

def entity_snapshot(db: Session, entity_type: str, entity_id: int) -> tuple[str, float]:
    """取单据的 (标题, 金额)。单据已删除时抛 ApprovalError。"""
    model = ENTITY_MODELS.get(entity_type)
    if model is None:
        raise ApprovalError(f"不支持的单据类型：{entity_type}")
    obj = db.get(model, entity_id)
    if obj is None:
        raise ApprovalError("单据不存在或已被删除")

    if entity_type == "supplier_payment":
        sup = obj.supplier.name if obj.supplier else "未指定收款方"
        title = f"付款 {sup} ¥{obj.amount:,.2f}"
    elif entity_type == "purchase_contract":
        title = f"采购合同 {obj.name}"
    elif entity_type == "sub_contract":
        title = f"分包合同 {obj.name}"
    else:
        title = f"费用 {obj.item_name} ¥{obj.amount:,.2f}"
    return title, float(obj.amount or 0)


def entity_project_id(db: Session, entity_type: str, entity_id: int):
    model = ENTITY_MODELS[entity_type]
    obj = db.get(model, entity_id)
    return getattr(obj, "project_id", None) if obj else None


# ---------------------------------------------------------------- 提交 / 审批

def submit(db: Session, entity_type: str, entity_id: int, user: User,
           comment: str = "", custom_steps: list | None = None) -> ApprovalRequest:
    """提交审批。同一单据只允许一个未终结的审批单；已驳回/已撤回的会重新提交。

    custom_steps：仅老板/admin 可用的「临时流程」，为这一张单据单独指定岗位链，
    覆盖该类型的默认流程。留空则用默认流程。
    """
    if entity_type not in ENTITY_MODELS:
        raise ApprovalError(f"不支持的单据类型：{entity_type}")

    if custom_steps is not None:
        if user.role not in ("老板", "admin"):
            raise ApprovalError("只有老板可以为单据指定临时审批流程")
        # 支持换行、逗号、中文顿号分隔，方便在单据页面一行内输入
        raw = []
        for line in custom_steps:
            raw.extend(re.split(r"[\n,，、;；]+", line))
        steps = [s.strip() for s in raw if s.strip()][:6]
        bad = [s for s in steps if s not in BUILTIN_ROLES]
        if bad:
            raise ApprovalError(f"未知岗位：{'、'.join(bad)}")
        steps = [s for s in steps if s in BUILTIN_ROLES][:6]
    else:
        steps = flow_steps(get_flow(db, entity_type))
    title, amount = entity_snapshot(db, entity_type, entity_id)
    project_id = entity_project_id(db, entity_type, entity_id)

    existing = db.query(ApprovalRequest).filter(
        ApprovalRequest.entity_type == entity_type,
        ApprovalRequest.entity_id == entity_id).first()

    # 进行中的单据不允许重复提交
    if existing and existing.status in ("待审批", "审批中"):
        raise ApprovalError("该单据已在审批中，请勿重复提交")

    if existing:
        # 驳回/撤回后重新提交：清空旧节点，重建流程。
        # 用 bulk delete + expire，而非遍历 relationship 或 .clear()：
        #   - 遍历 delete() 后旧对象仍留在 identity map，同一 session 内再读会拿到新旧混合集；
        #   - .clear() 会把随后新建的节点也一并摘掉，导致节点数为 0。
        db.query(ApprovalStep).filter(ApprovalStep.request_id == existing.id).delete(
            synchronize_session=False)
        db.flush()
        db.expire(existing, ["steps"])
        req = existing
        req.status = "待审批"
        req.result_note = ""
        req.finished_at = None
    else:
        req = ApprovalRequest(entity_type=entity_type, entity_id=entity_id)
        db.add(req)

    req.title = title
    req.amount = amount
    req.project_id = project_id
    req.submitted_by = user.id
    req.submitted_at = now()
    req.total_steps = len(steps)
    req.current_step = 0 if steps else -1
    req.result_note = comment
    db.flush()

    for i, role in enumerate(steps):
        db.add(ApprovalStep(request_id=req.id, step_no=i, role=role, status="待审批"))

    # 未配置任何节点 = 无需审批，直接通过
    if not steps:
        req.status = "已通过"
        req.current_step = 0
        req.finished_at = now()
        req.result_note = comment or "未配置审批流程，自动通过"

    _audit(db, user, req, "提交", comment)
    db.commit()
    return req


def act(db: Session, req: ApprovalRequest, user: User, approve: bool,
        comment: str = "") -> ApprovalRequest:
    """处理当前节点：通过则前进，驳回则整单终止。

    岗位校验放在服务层而不是只靠路由层——`can_approve_now` 是可选的旁路检查，
    任何绕过它直接调 act() 的代码（如脚本、将来的定时任务）都必须同样受约束。
    """
    if req.status not in ("待审批", "审批中"):
        raise ApprovalError(f"该审批单当前状态为「{req.status}」，无法操作")

    idx = req.current_step
    steps = sorted(req.steps, key=lambda s: s.step_no)
    if idx < 0 or idx >= len(steps):
        raise ApprovalError("审批流程异常：找不到当前节点")

    # 只有当前节点对应的岗位（或老板）才能处理
    step = steps[idx]
    if user.role not in ("老板", "admin") and step.role != user.role:
        raise ApprovalError(f"当前关卡需「{step.role}」审批，你是「{user.role}」，无权处理")

    if req.status == "待审批":
        req.status = "审批中"

    if not approve:
        step.status = "已驳回"
        step.actor_id = user.id
        step.actor_name = user.display_name or user.username
        step.comment = comment
        step.acted_at = now()
        req.status = "已驳回"
        req.finished_at = now()
        req.result_note = comment
        _audit(db, user, req, "驳回", comment)
        db.commit()
        return req

    step.status = "已通过"
    step.actor_id = user.id
    step.actor_name = user.display_name or user.username
    step.comment = comment
    step.acted_at = now()

    if idx + 1 >= len(steps):
        # 最后一个节点通过 → 整单通过（注意：不写任何金额）
        req.status = "已通过"
        req.current_step = len(steps)
        req.finished_at = now()
        req.result_note = comment
    else:
        req.current_step = idx + 1
        steps[idx + 1].status = "待审批"

    _audit(db, user, req, "通过", comment)
    db.commit()
    return req


def withdraw(db: Session, req: ApprovalRequest, user: User, comment: str = "") -> ApprovalRequest:
    """提交人撤回。只有未终结的单据可撤回。"""
    if req.status not in ("待审批", "审批中"):
        raise ApprovalError(f"该审批单已「{req.status}」，不能撤回")
    if req.submitted_by != user.id and user.role not in ("老板", "admin"):
        raise ApprovalError("只有提交人或老板可以撤回")
    req.status = "已撤回"
    req.finished_at = now()
    req.result_note = comment or "提交人撤回"
    _audit(db, user, req, "撤回", comment)
    db.commit()
    return req


def force_pass_all(db: Session, req: ApprovalRequest, user: User,
                   comment: str = "老板直接通过") -> ApprovalRequest:
    """老板一次性通过该单据的全部关卡。

    逐关调用 act() 会对每一步产生独立的审计记录，事后能看出是谁批的；
    这里批量处理时保留同样的留痕，只是把剩余关卡一次走完。
    """
    if user.role not in ("老板", "admin"):
        raise ApprovalError("只有老板可以一次性通过")
    guard = 0
    while req.status in ("待审批", "审批中") and guard < 10:
        act(db, req, user, True, comment)
        db.refresh(req)
        guard += 1
    return req


# ---------------------------------------------------------------- 查询辅助

def pending_for_role(db: Session, user: User) -> list[ApprovalRequest]:
    """当前用户「待我审批」的单据：节点岗位 = 用户岗位，且自己是当前节点。"""
    if not user:
        return []
    if user.role in ("老板", "admin"):
        # 老板看到全部在途的
        return db.query(ApprovalRequest).filter(
            ApprovalRequest.status.in_(["待审批", "审批中"])).all()
    out = []
    reqs = db.query(ApprovalRequest).filter(
        ApprovalRequest.status.in_(["待审批", "审批中"])).all()
    for r in reqs:
        steps = sorted(r.steps, key=lambda s: s.step_no)
        if 0 <= r.current_step < len(steps) and steps[r.current_step].role == user.role:
            out.append(r)
    return out


# ---------------------------------------------------------------- 入账口径

def booking_enabled(db: Session, entity_type: str) -> bool:
    """该单据类型是否启用了「未审批不入账」。

    三者同时满足才阻断：流程已启用、开关打开、类型在可控范围（付款/费用）。
    任一不满足 → 一律入账，保持历史行为不变。
    """
    if entity_type not in BLOCK_BOOKING_TYPES:
        return False
    f = get_flow(db, entity_type)
    return bool(f and f.enabled and f.block_booking and flow_steps(f))


def is_booked(db: Session, entity_type: str, entity_id: int) -> bool:
    """这笔单据现在该不该计入台账/利润。

    判定顺序（任一命中即入账）：
      1. 该类型未启用「未审批不入账」 → 入账（默认行为）
      2. 没有任何审批记录            → 入账（**存量数据视为已通过**）
      3. 审批状态为「已通过」        → 入账
    其余（待审批/审批中/已驳回/已撤回）不入账。
    """
    if not booking_enabled(db, entity_type):
        return True
    req = db.query(ApprovalRequest).filter(
        ApprovalRequest.entity_type == entity_type,
        ApprovalRequest.entity_id == entity_id).first()
    if req is None:
        return True          # 存量：从未提交过审批，视为已通过
    return req.status == "已通过"


def booked_ids(db: Session, entity_type: str, ids: list) -> set:
    """批量版：从一批 id 里筛出「应入账」的。

    聚合场景下逐条调 is_booked 会对每行发一次查询（N+1）。这里一次性把该类型的
    审批记录捞进内存再比对：
      - 有审批记录且已通过 → 入账
      - 有审批记录但未通过 → 不入账
      - 无审批记录（存量）  → 入账
    """
    ids = list(ids or [])
    if not ids or not booking_enabled(db, entity_type):
        return set(ids)
    blocked = {r.entity_id for r in db.query(ApprovalRequest).filter(
        ApprovalRequest.entity_type == entity_type,
        ApprovalRequest.entity_id.in_(ids),
        ApprovalRequest.status != "已通过").all()}
    return set(ids) - blocked


def filter_booked(db: Session, entity_type: str, rows: list, id_attr: str = "id") -> list:
    """从 ORM 对象列表里滤掉「不入账」的单据。台账循环里直接用。"""
    if not rows:
        return rows
    keep = booked_ids(db, entity_type, [getattr(r, id_attr) for r in rows])
    return [r for r in rows if getattr(r, id_attr) in keep]


def approval_state(db: Session, entity_type: str, entity_id: int) -> dict:
    """给业务页面用：该单据的审批状态摘要。"""
    r = db.query(ApprovalRequest).filter(
        ApprovalRequest.entity_type == entity_type,
        ApprovalRequest.entity_id == entity_id).first()
    if not r:
        return {"exists": False, "status": "", "label": "未提交"}
    label = {
        "待审批": "待提交审批", "审批中": "审批中", "已通过": "已通过",
        "已驳回": "已驳回", "已撤回": "已撤回",
    }.get(r.status, r.status)
    return {
        "exists": True, "id": r.id, "status": r.status, "label": label,
        "submitted_by": r.submitter.display_name or (r.submitter.username if r.submitter else ""),
        "submitted_at": r.submitted_at, "finished_at": r.finished_at,
        "current_step": r.current_step, "total_steps": r.total_steps,
        "result_note": r.result_note,
    }


def can_approve_now(req: ApprovalRequest, user: User) -> bool:
    """当前用户能否处理此单（是否为当前节点岗位，或老板）。"""
    if not user or req.status not in ("待审批", "审批中"):
        return False
    if user.role in ("老板", "admin"):
        return True
    steps = sorted(req.steps, key=lambda s: s.step_no)
    if 0 <= req.current_step < len(steps):
        return steps[req.current_step].role == user.role
    return False


def _audit(db: Session, user: User, req: ApprovalRequest, action: str, comment: str):
    db.add(AuditLog(
        user_id=user.id, username=user.username, action="审批",
        entity_type=f"审批-{ENTITY_LABELS.get(req.entity_type, req.entity_type)}",
        entity_id=req.id,
        detail=f"{action} {req.title}" + (f"｜意见：{comment}" if comment else ""),
    ))
