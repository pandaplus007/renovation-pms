"""变更签证（增项）路由。

这是装修项目利润的核心变量：合同签完到结算之间会冒出一堆增减项。
不把它们单独立账，毛利报表上的数字就只是「合同额 − 已下单」，
既可能偏高（未下单的钱没扣），也可能偏低（增项收的钱没算进收入）。

路由挂在 `/changes`，同时挂到项目详情页的标签里（`/projects/{id}/changes`）。
"""
from __future__ import annotations

import os
import urllib.parse
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import login_required
from .. import changes as chg
from ..database import get_db
from ..models import (CHANGE_CATEGORIES, CHANGE_STATUS, CHANGE_TRANSITIONS,
                      ChangeOrder, CostItem, Project, PurchaseOrder, SubContract,
                      User)
from ..utils.audit import log_audit
from ..utils.excel_export import export_xlsx
from ..utils.files import save_upload
from ..voided import not_voided
from ..web import company_info, render

router = APIRouter(tags=["changes"])


def _f(v: str | None, d=0.0) -> float:
    try:
        return float(v) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _d(v: str | None):
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return None


# ---------- 关联单据的候选列表 ----------
# 只列**本项目**的单据。跨项目关联几乎一定是挂错了——变更属于某个项目，
# 它引发的采购/分包/费用也必然属于同一个项目。筛选掉比事后报错好。

def _candidate_pos(db: Session, project_id: int) -> list[dict]:
    """本项目的采购单（带已付/未付，勾选时就能看出哪些还欠着）。"""
    rows = db.query(PurchaseOrder).filter(
        PurchaseOrder.project_id == project_id).order_by(
        PurchaseOrder.order_date.desc().nullslast()).all()
    amt = chg._po_amount_map(db, [o.id for o in rows])
    paid = chg._po_paid_map(db, [o.id for o in rows])
    out = []
    for o in rows:
        total = amt.get(o.id, 0.0)
        p = paid.get(o.id, 0.0)
        remain = round(total - p, 2)
        out.append({
            "id": o.id,
            "label": "#%d %s（¥%s，%s）"
                     % (o.id, o.order_no or "采购单", format(total, ",.2f"),
                        "已付清" if remain <= 0.5 else "欠 ¥%s" % format(remain, ",.2f")),
        })
    return out


def _candidate_scs(db: Session, project_id: int) -> list[dict]:
    """本项目的分包合同。"""
    rows = db.query(SubContract).filter(
        SubContract.project_id == project_id).order_by(SubContract.id.desc()).all()
    paid = chg._sc_paid_map(db, [o.id for o in rows])
    out = []
    for o in rows:
        p = paid.get(o.id, 0.0)
        remain = round(float(o.amount or 0) - p, 2)
        out.append({
            "id": o.id,
            "label": "%s（¥%s，%s）"
                     % (o.name or o.contract_no or "#%d" % o.id,
                        format(float(o.amount or 0), ",.2f"),
                        "已付清" if remain <= 0.5 else "欠 ¥%s" % format(remain, ",.2f")),
        })
    return out


def _candidate_costs(db: Session, project_id: int) -> list[dict]:
    """本项目的成本项（可关联到变更签证的候选）。

    排除作废项：作废的支出不该被关联进一张要业主签字、要进结算金额的变更单。
    """
    rows = db.query(CostItem).filter(
        CostItem.project_id == project_id, not_voided(CostItem)).order_by(
        CostItem.occurred_date.desc().nullslast()).all()
    return [{"id": o.id,
             "label": "%s · %s（¥%s）" % (o.category or "其他费用",
                                    o.item_name or "未命名", format(o.amount, ",.2f"))}
            for o in rows]


# ================================================================== 列表

@router.get("/changes")
def change_list(request: Request, status: str = "", project_id: int = 0,
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    """变更签证总览：按项目聚合，一眼看出各项目增了多少。"""
    rows = chg.all_projects_with_changes(db)
    if project_id:
        rows = [r for r in rows if r["project"].id == project_id]
    if status:
        rows = [r for r in rows if any(
            o.status == status for o in chg.rows_for_project(db, r["project"].id))]
    projects = db.query(Project).order_by(Project.name).all()
    t_add = sum(r["add"] for r in rows)
    t_cut = sum(r["cut"] for r in rows)
    t_conf = sum(r["confirmed"] for r in rows)
    t_cost = sum(r["cost"] for r in rows)
    return render(request, "changes/list.html", {
        "active": "changes", "user": user, "rows": rows, "projects": projects,
        "status": status, "project_id": project_id,
        "change_status": CHANGE_STATUS,
        "totals": {
            "add": round(t_add, 2), "cut": round(t_cut, 2),
            "confirmed": round(t_conf, 2), "cost": round(t_cost, 2),
            "profit": round(t_conf - t_cost, 2),
            "pending_count": sum(r["pending_count"] for r in rows),
            "pending_amount": round(sum(r["pending_amount"] for r in rows), 2),
            # 已报给业主等签字的子集——能拿去催的金额
            "awaiting_sign_count": sum(r.get("awaiting_sign_count", 0)
                                       for r in rows),
            "awaiting_sign_amount": round(sum(
                r.get("awaiting_sign_amount", 0.0) for r in rows), 2),
            "count": sum(r["count"] for r in rows),
        },
    })


@router.get("/changes/export")
def change_list_export(request: Request, db: Session = Depends(get_db),
                       user: User = Depends(login_required)):
    data = []
    for p in db.query(Project).order_by(Project.id).all():
        for o in chg.rows_for_project(db, p.id):
            data.append([
                p.name, o.change_no, o.title, o.category, o.status,
                o.raise_date or "", o.confirm_date or "", o.signer,
                o.amount, o.cost, o.profit, o.reason, o.note,
                # 留痕与定价依据一起导出：台账被拿去做对账或扯皮时，
                # 光有金额没有「谁批的、凭什么批、为什么否」是无用的
                o.pricing_note, o.submitted_by, o.approved_by,
                o.settled_by, o.reject_reason,
            ])
    return export_xlsx(
        "变更签证台账.xlsx",
        ["项目", "签证单号", "变更事项", "类别", "状态", "提出日期", "确认日期",
         "甲方签字人", "变更金额", "对应成本", "变更毛利", "变更原因", "备注",
         "定价依据", "报审人", "批准人", "结算确认人", "驳回理由"],
        data, "变更签证",
    )


# ================================================================== 项目内

@router.get("/projects/{project_id}/changes")
def project_changes(request: Request, project_id: int,
                    db: Session = Depends(get_db), user: User = Depends(login_required)):
    p = db.get(Project, project_id)
    if p is None:
        return render(request, "changes/project.html", {
            "active": "changes", "user": user, "project": None, "rows": [],
            "cats": [], "summary": {}, "cat_summary": [],
        })
    ct = chg.project_change_totals(db).get(p.id, {}) or {}
    rows = chg.rows_for_project(db, p.id)
    return render(request, "changes/project.html", {
        "active": "changes", "user": user, "project": p,
        "rows": rows,
        "cats": chg.category_summary(db, p.id),
        "cat_summary": chg.category_summary(db, p.id),
        "statuses": CHANGE_STATUS, "categories": CHANGE_CATEGORIES,
        "transitions": CHANGE_TRANSITIONS,
        # 每行的下一步可选项——UI 不用自己判断该不该给「确认」按钮，
        # 后端也已经用同一份 transitions 做了校验，两边不会打架
        "next_status": {r.id: sorted(CHANGE_TRANSITIONS.get(r.status, set()))
                        for r in rows},
        # 待签字金额单独列出：已报给业主等签字的笔钱是能拿去催的，
        # 和「还在内部拟稿」不是一回事
        "awaiting_sign": [r for r in rows if r.status in chg.AWAITING_SIGN_STATUS],
        "summary": ct,
        "settle": chg.settle_amount(db, p.id, p.contract),
    })


# ================================================================== 增删改

@router.get("/changes/new")
def change_new(request: Request, project_id: int = 0,
               db: Session = Depends(get_db), user: User = Depends(login_required)):
    p = db.get(Project, project_id) if project_id else None
    return render(request, "changes/form.html", {
        "active": "changes", "user": user, "obj": None, "project": p,
        "projects": db.query(Project).order_by(Project.name).all(),
        "statuses": CHANGE_STATUS, "categories": CHANGE_CATEGORIES,
        "transitions": CHANGE_TRANSITIONS,
        "today": date.today(),
    })


@router.get("/changes/{cid}/edit")
def change_edit(request: Request, cid: int, db: Session = Depends(get_db),
                user: User = Depends(login_required)):
    obj = db.get(ChangeOrder, cid)
    if obj is None:
        return RedirectResponse("/changes", status_code=303)
    p = db.get(Project, obj.project_id)
    return render(request, "changes/form.html", {
        "active": "changes", "user": user, "obj": obj, "project": p,
        "projects": db.query(Project).order_by(Project.name).all(),
        "statuses": CHANGE_STATUS, "categories": CHANGE_CATEGORIES,
        "transitions": CHANGE_TRANSITIONS,
        "today": date.today(),
        # 转「已结算」要做什么：把检查项清单先算好，模板直接显示。
        # 只有当前状态是「已确认」时才需要算——别的状态转不了结算。
        "settle_checks": (chg.settle_readiness(db, obj)
                          if obj.status == "已确认" else None),
        # 关联选择器的候选：只列本项目的单据，避免跨项目挂错
        "cand_po": _candidate_pos(db, obj.project_id),
        "cand_sc": _candidate_scs(db, obj.project_id),
        "cand_cost": _candidate_costs(db, obj.project_id),
    })


@router.post("/changes/save")
def change_save(request: Request, cid: int = Form(0),
                project_id: int = Form(0),
                title: str = Form(""), category: str = Form("其他"),
                amount: str = Form("0"), cost: str = Form("0"),
                status: str = Form("草稿"), change_no: str = Form(""),
                raise_date: str = Form(""), confirm_date: str = Form(""),
                signer: str = Form(""), reason: str = Form(""), note: str = Form(""),
                file_path: str = Form(""),
                pricing_note: str = Form(""),
                reject_reason: str = Form(""),
                linked_po: list[str] = Form([]),
                linked_sc: list[str] = Form([]),
                linked_cost: list[str] = Form([]),
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    pid = int(project_id or 0)
    if pid <= 0:
        return RedirectResponse("/changes/new", status_code=303)
    obj = db.get(ChangeOrder, int(cid)) if cid else None
    is_new = obj is None
    if obj is None:
        obj = ChangeOrder(project_id=pid)
        db.add(obj)
    old_status = None if is_new else obj.status
    new_status = (status or "草稿").strip()

    # ---- 状态流转校验 ----
    # 表单里可以直接选任意状态，但状态机是有方向的：不能把「已结算」
    # 一把拉回「草稿」（钱已经结过了，改回去会让结算金额凭空变动）。
    # 非法流转不静默改回旧值，而是带着错误提示回表单——静默回退会
    # 让人以为保存成功了，实际改的是另一个状态。
    illegal = None
    if not is_new and new_status != old_status:
        if not chg.can_transition(old_status, new_status):
            illegal = (old_status, new_status)
            new_status = old_status

    obj.project_id = pid
    obj.title = title.strip() or "未命名变更"
    obj.category = category or "其他"
    obj.amount = _f(amount)
    obj.cost = abs(_f(cost))        # 成本恒为正：填负数是手误，不该当成减项成本
    obj.status = new_status
    obj.change_no = change_no.strip()
    obj.raise_date = _d(raise_date)
    obj.confirm_date = _d(confirm_date)
    obj.signer = signer.strip()
    obj.reason = reason.strip()
    obj.note = note.strip()
    obj.file_path = file_path
    obj.pricing_note = pricing_note.strip()

    # 驳回理由：进「已驳回」时必填。驳回是流程内的正式结论，
    # 没有理由的驳回过三个月没人记得为什么否，而下次很可能重犯。
    obj.reject_reason = reject_reason.strip()
    if new_status == "已驳回" and not obj.reject_reason:
        obj.reject_reason = "（未填理由）"
    elif new_status != "已驳回":
        obj.reject_reason = ""      # 不再是驳回状态就别留着旧理由

    # ---- 关联执行单据 ----
    obj.linked_po_ids = ChangeOrder._ids_to_json(linked_po)
    obj.linked_sc_ids = ChangeOrder._ids_to_json(linked_sc)
    obj.linked_cost_ids = ChangeOrder._ids_to_json(linked_cost)

    # ---- 审批留痕：谁做的这一步，什么时候 ----
    # 原来「已确认」只是一个按钮，事后只知道状态变了，不知道谁批的。
    # 签字单是法律凭据，这几个字段是它的数字化对应物。
    me = (getattr(user, "display_name", "") or getattr(user, "username", "")
          or "")
    if new_status == "已定价待业主审" and not is_new \
            and old_status != "已定价待业主审":
        obj.submitted_by = me
        obj.submitted_at = datetime.now()
    if new_status == "已确认" and old_status != "已确认":
        obj.approved_by = obj.signer.strip() or me
        if not obj.confirm_date:
            obj.confirm_date = date.today()
    if new_status == "已结算" and old_status != "已结算":
        obj.settled_by = me

    db.commit()

    detail = f"{obj.title} 金额 {obj.amount:,.2f} 状态 {obj.status}"
    if illegal:
        detail += "（状态从「%s」改到「%s」被拒，已保持原状态）" % illegal
    if new_status == "已驳回":
        detail += " 驳回理由：%s" % obj.reject_reason
    log_audit(db, user, "新增" if is_new else "修改", "变更签证", obj.id, detail)

    # 同步结算金额（仅在未人工填过 settle_amount 时才写回，见 changes.sync_settle_amount）
    chg.sync_settle_amount(db, pid)

    if illegal:
        msg = "不能从「%s」直接改成「%s」。可选的下一步：%s" % (
            illegal[0], illegal[1],
            "、".join(sorted(CHANGE_TRANSITIONS.get(illegal[0], set()))) or "无（终态）")
        return RedirectResponse(
            f"/changes/{obj.id}/edit?err={urllib.parse.quote(msg)}",
            status_code=303)
    return RedirectResponse(f"/projects/{pid}/changes", status_code=303)


@router.post("/changes/{cid}/delete")
def change_delete(request: Request, cid: int, db: Session = Depends(get_db),
                  user: User = Depends(login_required)):
    obj = db.get(ChangeOrder, cid)
    if obj is None:
        return RedirectResponse("/changes", status_code=303)
    pid = obj.project_id
    db.delete(obj)
    db.commit()
    log_audit(db, user, "删除", "变更签证", cid, obj.title)
    chg.sync_settle_amount(db, pid)
    return RedirectResponse(f"/projects/{pid}/changes", status_code=303)


# ================================================================== 签字扫描件
#
# 为什么单独一个上传路由，而不是在 /changes/save 里带 file 字段：
# save 是表单提交（multipart 会让所有字段都要从 Form 读），而扫描件是
# 独立动作——同一张签证可能先传草稿版、确认后再传签字版，分两次传，
# 每次都要单独留痕（谁传的、什么时候、哪个文件）。

# 纸质签名扫描件：图片或 PDF。不收 doc/docx——签字件是签过字的
# 固定版式，转成可编辑格式就失去了「签过」的证据价值。
SCAN_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


@router.post("/changes/{cid}/upload")
async def change_upload(request: Request, cid: int,
                        file: UploadFile = File(None),
                        db: Session = Depends(get_db), user: User = Depends(login_required)):
    """上传签字扫描件。每张签证单保留一份当前件，但每次上传都写审计日志。

    覆盖上传时**旧文件不删**：扫描件是法律凭据，删了就只剩最新一版，
    而事后扯皮时需要能说清「当时交的是哪一份」。物理文件留在磁盘上，
    审计日志记着它的名字——回溯靠的是日志，不是目录。
    """
    obj = db.get(ChangeOrder, cid)
    if obj is None:
        return RedirectResponse("/changes", status_code=303)

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in SCAN_EXTS:
        flash = f"不支持的文件类型「{ext or '无后缀'}」，只收 PDF 与图片"
        return RedirectResponse(
            f"/changes/{cid}/edit?err={urllib.parse.quote(flash)}", status_code=303)

    old_name = obj.file_name or obj.file_path
    url = await save_upload(file, "changes", SCAN_EXTS)
    if not url:
        flash = "上传失败：可能是类型不允许或超过大小上限"
        return RedirectResponse(
            f"/changes/{cid}/edit?err={urllib.parse.quote(flash)}", status_code=303)

    obj.file_path = url
    obj.file_name = file.filename or ""
    obj.file_uploaded_at = datetime.now()
    obj.file_uploaded_by = (getattr(user, "display_name", "")
                            or getattr(user, "username", "") or "")
    db.commit()
    detail = f"{obj.title}：{obj.file_name}"
    if old_name and old_name != obj.file_name:
        detail += f"（覆盖了原来的 {old_name}）"
    log_audit(db, user, "上传扫描件", "变更签证", obj.id, detail)
    return RedirectResponse(f"/changes/{cid}/edit?ok=1", status_code=303)


@router.post("/changes/{cid}/scan-delete")
def change_scan_delete(request: Request, cid: int,
                       db: Session = Depends(get_db), user: User = Depends(login_required)):
    """解除扫描件关联（只清关联，不删磁盘文件）。

    刻意**不删物理文件**：扫描件是签过字的凭据，误删无法恢复。
    真要清理磁盘得有人明确下指令，不能由一个「删除」按钮顺带做掉。
    """
    obj = db.get(ChangeOrder, cid)
    if obj is None:
        return RedirectResponse("/changes", status_code=303)
    name = obj.file_name or "（未记录文件名）"
    obj.file_path = ""
    obj.file_name = ""
    obj.file_uploaded_at = None
    obj.file_uploaded_by = ""
    db.commit()
    log_audit(db, user, "删除扫描件", "变更签证", cid,
              f"{obj.title}：解除关联 {name}（磁盘文件保留）")
    return RedirectResponse(f"/changes/{cid}/edit", status_code=303)


# ================================================================== 纸质多联单

# 三联的用途是固定的：甲方留档、乙方留档、现场/财务存档。
# 不做成可配置——联数一旦能改，打印出来几联全靠人记，反而容易漏联。
COPY_LABELS = ("甲方留存", "乙方留存", "现场存档")


@router.get("/changes/{cid}/print")
def change_print(request: Request, cid: int, copies: int = 3,
                 db: Session = Depends(get_db), user: User = Depends(login_required)):
    """变更签证单纸质多联打印页。

    用浏览器打印而不是服务端生成 PDF，原因：
      1. 容器是 python:3.13-slim，**没有任何中文字体**。用 fpdf2/reportlab
         生成必须往镜像里塞一个 10MB+ 的 CJK 字体文件，否则中文全是方块。
      2. 加字体= 改 requirements + 重建镜像，NAS 上每次部署多一个失败点。
      3. 浏览器打印直接用用户系统里的中文字体，排版效果最好。
      4. 这份单子的用途就是出纸质件（签字、盖章、双方各执一份），
         本来就要经过打印机，不需要一个能邮件附件的 .pdf。
    """
    obj = db.get(ChangeOrder, cid)
    if obj is None:
        return RedirectResponse("/changes", status_code=303)
    copies = max(1, min(int(copies or 3), 6))
    p = db.get(Project, obj.project_id)
    ct = p.contract if p else None
    labels = list(COPY_LABELS[:copies])
    while len(labels) < copies:          # 超过 3 联时补「备用留存」
        labels.append("备用留存")
    return render(request, "changes/print.html", {
        "active": "changes", "user": user,
        "obj": obj, "project": p, "contract": ct,
        # 施工单位是公司品牌信息（company.json），不是采购合同的名称字段——
        # 早先误用 contract.contract_name，那栏印出来是空的。
        "company": company_info(),
        "copies": copies, "copy_labels": labels,
        # 打印页不套应用外壳，否则侧栏/顶栏会混进纸面（base.html 的
        # @media print 已经把它们隐藏，但独立页面连那层 DOM 都不生成）
        "standalone": True,
    })


@router.post("/changes/{cid}/transition")
def change_transition(request: Request, cid: int,
                      to: str = Form(""),
                      force: str = Form(""),
                      reject_reason: str = Form(""),
                      db: Session = Depends(get_db),
                      user: User = Depends(login_required)):
    """状态流转。含「已确认 → 已结算」的前置检查。

    三件事按顺序：
      1. 校验流转合法性（状态机，见 models.CHANGE_TRANSITIONS）
      2. 驳回必须填理由
      3. 转「已结算」时先跑 settle_readiness()，不通过就把缺什么
         带着 force=1 一起返回；用户点确认后带 force 再提交一次即放行

    **不硬拦**（用户明确要求「提示后可强制转」）：现实中常有
    「这笔变更不走采购」或「分包现金结算」的情况，硬拦会把流程卡死。
    强制放行会写审计日志，记下是谁强转的、当时缺什么。
    """
    obj = db.get(ChangeOrder, cid)
    if obj is None:
        return RedirectResponse("/changes", status_code=303)
    to = (to or "").strip()
    old = obj.status

    # ---- 1. 流转合法性 ----
    if not chg.can_transition(old, to):
        msg = "不能从「%s」变成「%s」。当前状态允许的下一步：%s" % (
            old, to,
            "、".join(sorted(CHANGE_TRANSITIONS.get(old, set()))) or "无（已是终态）")
        return RedirectResponse(
            f"/changes/{cid}/edit?err={urllib.parse.quote(msg)}", status_code=303)

    # ---- 2. 驳回必填理由 ----
    if to == "已驳回":
        reason = reject_reason.strip()
        if not reason:
            msg = "驳回必须填理由——否则过三个月没人记得为什么否，下次很可能重犯"
            return RedirectResponse(
                f"/changes/{cid}/edit?err={urllib.parse.quote(msg)}",
                status_code=303)
        obj.reject_reason = reason
    elif old == "已驳回":
        # 离开驳回态就把理由清掉。留着它会出事：单子退回拟稿→改价→重新报审，
        # 旧理由还挂在记录上，看的人会以为「上次的结论还没解决」，
        # 而实际上一轮已经被撤销了。历史在审计日志里，不在当前状态上。
        obj.reject_reason = ""

    # ---- 3. 转结算前的检查 ----
    forced_note = ""
    if to == "已结算":
        chk = chg.settle_readiness(db, obj)
        if not chk["ready"] and not force:
            missing = [i for i in chk["items"] if not i["ok"]]
            msg = "还不能标记为已结算：" + "；".join(
                "%s（%s）" % (i["label"], i["detail"]) for i in missing)
            return RedirectResponse(
                f"/changes/{cid}/settle-check?missing={urllib.parse.quote(msg)}",
                status_code=303)
        if not chk["ready"]:
            forced_note = " 强制放行，缺：" + "；".join(
                "%s（%s）" % (i["label"], i["detail"])
                for i in chk["items"] if not i["ok"])
        obj.settled_by = (getattr(user, "display_name", "")
                          or getattr(user, "username", "") or "")

    me = (getattr(user, "display_name", "") or getattr(user, "username", "")
          or "")
    obj.status = to
    if to == "已定价待业主审":
        obj.submitted_by = obj.submitted_by or me
        obj.submitted_at = obj.submitted_at or datetime.now()
    if to == "已确认":
        obj.approved_by = obj.approved_by or obj.signer.strip() or me
        obj.confirm_date = obj.confirm_date or date.today()
    db.commit()

    action = "结算" if to == "已结算" else ("驳回" if to == "已驳回" else "流转")
    log_audit(db, user, action, "变更签证", cid,
              "%s：%s → %s%s" % (obj.title, old, to, forced_note))
    chg.sync_settle_amount(db, obj.project_id)
    return RedirectResponse(f"/projects/{obj.project_id}/changes", status_code=303)


@router.get("/changes/{cid}/settle-check")
def change_settle_check(request: Request, cid: int,
                        missing: str = "", force: str = "",
                        db: Session = Depends(get_db),
                        user: User = Depends(login_required)):
    """转「已结算」前的检查页：逐项列出缺什么 + 强制放行按钮。

    单独一个页面而不是弹 alert：检查项可能有 5-6 条，
    alert 里放不下也看不清，而「缺了什么」正是需要人对着核的信息。
    """
    obj = db.get(ChangeOrder, cid)
    if obj is None:
        return RedirectResponse("/changes", status_code=303)
    chk = chg.settle_readiness(db, obj)
    if obj.status != "已确认":
        return RedirectResponse(
            f"/changes/{cid}/edit?err={urllib.parse.quote('当前状态是「%s」，只有「已确认」才能转已结算' % obj.status)}",
            status_code=303)
    return render(request, "changes/settle_check.html", {
        "active": "changes", "user": user, "obj": obj,
        "project": db.get(Project, obj.project_id),
        "checks": chk["items"], "ready": chk["ready"],
        "missing_text": missing,
    })


# 保留旧路径做兼容：老模板/收藏还在用 /changes/{id}/confirm
@router.post("/changes/{cid}/confirm")
def change_confirm(request: Request, cid: int,
                   to: str = Form("已确认"),
                   db: Session = Depends(get_db), user: User = Depends(login_required)):
    obj = db.get(ChangeOrder, cid)
    if obj is None:
        return RedirectResponse("/changes", status_code=303)
    # 老接口的语义是「确认」；非法流转（已结算不能退回确认）转去检查页
    if not chg.can_transition(obj.status, to):
        return RedirectResponse(f"/changes/{cid}/settle-check", status_code=303)
    return change_transition(request, cid, to=to, force="", reject_reason="",
                             db=db, user=user)