"""审批中心路由：待办、审批单管理、流程配置。

权限：整块受 permissions.py 的 approvals 模块控制，路由动作已登记在
`_route_actions.json`。`role_guard` 会在进入前拦下无权限的请求。
"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..approvals import (ApprovalError, ENTITY_LABELS, act, can_approve_now,
                         flow_steps, force_pass_all, get_flow, is_enabled,
                         pending_for_role, submit, withdraw)
from ..auth import login_required
from ..database import get_db
from ..models import BLOCK_BOOKING_TYPES, ApprovalFlow, ApprovalRequest, User, now
from ..permissions import BUILTIN_ROLES
from ..utils.audit import log_audit
from ..web import render

router = APIRouter()

# 状态 → badge 类名。**不再自定义映射**：统一走 app.state_colors 的 4 档语义色，
# 与其他页面保持一致（原先这里把「已驳回」标成 danger 红色，与「变更签证已驳回」
# 用 secondary 不一致——同一个状态词在两个页面颜色不同）。
STATUS_BADGE = {
    "待审批": "state-badge state-watch",
    "审批中": "state-badge state-active",
    "已通过": "state-badge state-done",
    "已驳回": "state-badge state-idle",
    "已撤回": "state-badge state-idle",
}


def _ctx(request: Request, db: Session, user: User, flash: str = "") -> dict:
    """审批中心共享上下文。未终结的单据在途，已终结的在下方归档区。"""
    inflight = db.query(ApprovalRequest).filter(
        ApprovalRequest.status.in_(["待审批", "审批中"])).order_by(
        ApprovalRequest.submitted_at.desc()).all()
    archived = db.query(ApprovalRequest).filter(
        ApprovalRequest.status.notin_(["待审批", "审批中"])).order_by(
        ApprovalRequest.finished_at.desc()).limit(50).all()
    return {
        "active": "approvals", "user": user, "flash": flash,
        "pending": pending_for_role(db, user),
        "inflight": inflight,
        "archived": archived,
        "entity_labels": ENTITY_LABELS,
        "status_badge": STATUS_BADGE,
        "can_approve": lambda r: can_approve_now(r, user),
    }


# ---------------------------------------------------------------- 审批中心

@router.get("/approvals")
def approvals_home(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(login_required)):
    return render(request, "approvals/list.html", _ctx(request, db, user, request.query_params.get("msg", "")))


# 注意：/approvals/flows 必须注册在 /approvals/{req_id} 之前，
# 否则 FastAPI 按定义顺序匹配，"flows" 会被当成 req_id 报 422。
@router.get("/approvals/flows")
def flows_page(request: Request, db: Session = Depends(get_db),
               user: User = Depends(login_required)):
    ctx = _ctx(request, db, user, request.query_params.get("msg", ""))
    ctx["active"] = ""
    ctx["roles"] = BUILTIN_ROLES
    ctx["flow_rows"] = [
        {
            "entity_type": key, "label": label,
            "flow": get_flow(db, key),
            "steps": flow_steps(get_flow(db, key)),
            "enabled": bool(get_flow(db, key) and get_flow(db, key).enabled),
            "block_booking": bool(get_flow(db, key) and get_flow(db, key).block_booking),
            "can_block": key in BLOCK_BOOKING_TYPES,
        }
        for key, label in ENTITY_LABELS.items()
    ]
    return render(request, "approvals/flows.html", ctx)


@router.get("/approvals/{req_id}")
def approval_detail(request: Request, req_id: int, db: Session = Depends(get_db),
                    user: User = Depends(login_required)):
    req = db.get(ApprovalRequest, req_id)
    if req is None:
        return RedirectResponse("/approvals?msg=审批单不存在", status_code=303)
    ctx = _ctx(request, db, user)
    ctx["active"] = ""
    ctx["req"] = req
    ctx["steps"] = sorted(req.steps, key=lambda s: s.step_no)
    ctx["detail_can_approve"] = can_approve_now(req, user)
    return render(request, "approvals/detail.html", ctx)


# ---------------------------------------------------------------- 审批动作

def _redirect(request: Request, msg: str, fallback: str = "/approvals"):
    """回跳到来源页（业务页面提交审批后应回到原单据），带提示。"""
    from urllib.parse import quote
    ref = request.headers.get("referer")
    base = ref or fallback
    return RedirectResponse(f"{base}#{quote(msg)}" if ref else f"{base}?msg={quote(msg)}",
                            status_code=303)


@router.post("/approvals/{req_id}/approve")
def approve(request: Request, req_id: int, comment: str = Form(""),
            db: Session = Depends(get_db), user: User = Depends(login_required)):
    req = db.get(ApprovalRequest, req_id)
    if req is None:
        return RedirectResponse("/approvals?msg=审批单不存在", status_code=303)
    if not can_approve_now(req, user):
        return RedirectResponse(f"/approvals?msg={ApprovalError('你没有当前节点的审批权限')}", status_code=303)
    try:
        act(db, req, user, True, comment)
    except ApprovalError as e:
        return RedirectResponse(f"/approvals?msg={e}", status_code=303)
    return RedirectResponse(f"/approvals?msg=已通过", status_code=303)


@router.post("/approvals/{req_id}/reject")
def reject(request: Request, req_id: int, comment: str = Form(""),
           db: Session = Depends(get_db), user: User = Depends(login_required)):
    req = db.get(ApprovalRequest, req_id)
    if req is None:
        return RedirectResponse("/approvals?msg=审批单不存在", status_code=303)
    if not can_approve_now(req, user):
        return RedirectResponse("/approvals?msg=你没有当前节点的审批权限", status_code=303)
    try:
        act(db, req, user, False, comment)
    except ApprovalError as e:
        return RedirectResponse(f"/approvals?msg={e}", status_code=303)
    return RedirectResponse("/approvals?msg=已驳回", status_code=303)


@router.post("/approvals/{req_id}/withdraw")
def withdraw_route(request: Request, req_id: int, comment: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    req = db.get(ApprovalRequest, req_id)
    if req is None:
        return RedirectResponse("/approvals?msg=审批单不存在", status_code=303)
    try:
        withdraw(db, req, user, comment)
    except ApprovalError as e:
        return RedirectResponse(f"/approvals?msg={e}", status_code=303)
    return RedirectResponse("/approvals?msg=已撤回", status_code=303)


@router.post("/approvals/{req_id}/resubmit")
def resubmit(request: Request, req_id: int, comment: str = Form(""),
             db: Session = Depends(get_db), user: User = Depends(login_required)):
    """驳回/撤回后重新提交。"""
    req = db.get(ApprovalRequest, req_id)
    if req is None:
        return RedirectResponse("/approvals?msg=审批单不存在", status_code=303)
    if req.submitted_by != user.id and user.role not in ("老板", "admin"):
        return RedirectResponse("/approvals?msg=只有提交人或老板可以重新提交", status_code=303)
    try:
        submit(db, req.entity_type, req.entity_id, user, comment)
    except ApprovalError as e:
        return RedirectResponse(f"/approvals?msg={e}", status_code=303)
    return RedirectResponse("/approvals?msg=已重新提交", status_code=303)


# ---------------------------------------------------------------- 流程配置

@router.post("/approvals/flows")
def save_flow(request: Request, entity_type: str = Form(...), steps: str = Form(""),
              enabled: str = Form(""), block_booking: str = Form(""),
              db: Session = Depends(get_db), user: User = Depends(login_required)):
    """保存某类单据的审批岗位链。steps 为每行一个岗位，最多 6 关。

    block_booking：仅对付款/费用有意义，打开后未审批通过的单据不计入台账与利润。
    """
    if entity_type not in ENTITY_LABELS:
        return RedirectResponse("/approvals/flows?msg=无效的单据类型", status_code=303)

    chain = [s.strip() for s in (steps or "").splitlines() if s.strip()][:6]
    unknown = [s for s in chain if s not in BUILTIN_ROLES]
    if unknown:
        return RedirectResponse(
            f"/approvals/flows?msg=未知岗位：{'、'.join(unknown)}", status_code=303)

    flow = get_flow(db, entity_type)
    if flow is None:
        flow = ApprovalFlow(entity_type=entity_type)
        db.add(flow)
    flow.name = f"{ENTITY_LABELS[entity_type]}审批"
    flow.steps = json.dumps(chain, ensure_ascii=False)
    flow.enabled = bool(enabled)
    # 合同类不支持阻断入账（合同额是签约额度，阻断会让记账严重滞后）
    flow.block_booking = bool(block_booking) and entity_type in BLOCK_BOOKING_TYPES
    flow.updated_at = now()
    db.commit()

    log_audit(db, user, "审批", f"审批流程-{ENTITY_LABELS[entity_type]}", flow.id,
              f"节点：{' → '.join(chain) if chain else '（无）'}｜启用={bool(enabled)}"
              f"｜未审批不入账={flow.block_booking}")
    return RedirectResponse("/approvals/flows?msg=已保存", status_code=303)


# ---------------------------------------------------------------- 业务单据提交

@router.post("/approvals/submit")
def submit_entity(request: Request, entity_type: str = Form(...), entity_id: int = Form(...),
                  comment: str = Form(""), custom_steps: str = Form(""),
                  db: Session = Depends(get_db), user: User = Depends(login_required)):
    """付款/合同/费用页面上点「提交审批」后走这里。

    custom_steps 非空时（每行一个岗位）走「临时流程」，仅老板/admin 有效。
    """
    chain = [s.strip() for s in re.split(r"[\n,，、;；]+", custom_steps or "") if s.strip()]
    # 老板可以绕过「该类型未启用流程」的限制直接提交（配合自定义流程使用）
    if not is_enabled(db, entity_type) and not chain:
        return _redirect(request, "该单据未启用审批流程")
    try:
        submit(db, entity_type, entity_id, user, comment,
               custom_steps=chain if chain else None)
        return _redirect(request, "已提交审批" + (f"（临时流程 {len(chain)} 关）" if chain else ""))
    except ApprovalError as e:
        return _redirect(request, str(e))


@router.post("/approvals/{req_id}/force-pass")
def force_pass(request: Request, req_id: int, comment: str = Form(""),
               db: Session = Depends(get_db), user: User = Depends(login_required)):
    """老板一次性通过全部关卡。"""
    req = db.get(ApprovalRequest, req_id)
    if req is None:
        return RedirectResponse("/approvals?msg=审批单不存在", status_code=303)
    try:
        force_pass_all(db, req, user, comment or "老板直接通过")
    except ApprovalError as e:
        return RedirectResponse(f"/approvals?msg={e}", status_code=303)
    return RedirectResponse("/approvals?msg=已全部通过", status_code=303)
