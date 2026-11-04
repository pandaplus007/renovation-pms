"""全局任务页与「我的任务」。

为什么要有这个页
--------------
`Task` 表早就存在，但只能从**项目详情页的第 12 个 Tab** 进入。实际使用的结果
是：任务建了，没人回去看；员工打开系统看到的是全公司几百条记录，
不知道哪 3 条是自己的事。

这个页面提供「按人 / 按时间 / 按状态」三个维度的横向视图，
以及一个明确的「我的任务」入口——这才是让任务真正被用起来的地方。

与项目详情页的关系
----------------
**双向链接**：这里的每条都能一键跳到对应项目，项目的任务 Tab 也回链到这里。
不重复实现编辑逻辑，写操作全部跳到项目页（保证只有一处真相）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import TASK_STATUS, Staff, Task, User
from ..utils.audit import log_audit
from ..web import render

router = APIRouter(prefix="/tasks", tags=["tasks"])

# 「我的任务」判定用哪些状态算未完成。未完成 = 还占着我的事。
OPEN_STATUS = tuple(s for s in TASK_STATUS if s not in ("已完成", "已取消"))


def _date(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _days_overdue(d) -> int | None:
    if not d:
        return None
    if isinstance(d, datetime):
        d = d.date()
    return (date.today() - d).days


def _fmt(d) -> str:
    if not d:
        return "—"
    if isinstance(d, datetime):
        d = d.date()
    return str(d)


def _match_user(db: Session, user: User, assignee: str, assignee_user_id: int,
                who: str, q: str) -> list:
    """按「负责人」筛任务。

    两种输入并存，所以筛选也要两者都认：
      - `mine=1` → 当前登录账号负责的（assignee_user_id 精确匹配）
      - `assignee=姓名` → 自由文本匹配（历史数据只有这个）
    「我的任务」同时带上用户名/昵称的模糊匹配，这样**历史任务也能被认领**——
    它们 assignee_user_id 是 NULL，但 assignee 里写着「张工」。
    """
    rows = db.query(Task)
    if who == "mine":
        names = {n for n in (user.display_name, user.username) if n}
        cond = Task.assignee_user_id == user.id
        if names:
            cond = or_(cond, Task.assignee.in_(names))
        rows = rows.filter(cond)
    elif who == "overdue":
        rows = rows.filter(Task.due_date.isnot(None),
                           Task.due_date < date.today(),
                           Task.status.notin_(("已完成", "已取消")))
    elif who == "today":
        rows = rows.filter(Task.due_date == date.today())
    elif who == "week":
        rows = rows.filter(Task.due_date.isnot(None),
                           Task.due_date >= date.today(),
                           Task.due_date <= date.today() + timedelta(days=7))
    elif who == "none":
        rows = rows.filter(Task.assignee == "", Task.assignee_user_id.is_(None))
    if assignee:
        rows = rows.filter(Task.assignee.like(f"%{assignee}%"))
    if q:
        like = f"%{q}%"
        rows = rows.filter(or_(Task.title.like(like), Task.note.like(like)))
    return rows


@router.get("")
def task_list(request: Request, who: str = "", status: list[str] = Query([]),
               assignee: str = "", q: str = "",
               db: Session = Depends(get_db), user: User = Depends(login_required)):
    rows = _match_user(db, user, assignee, 0, who, q)
    if status:
        rows = rows.filter(Task.status.in_(status))
    tasks = rows.order_by(Task.due_date.is_(None), Task.due_date,
                          Task.id.desc()).all()

    # 统计基于**当前筛选结果**，而不是全库——否则「我的任务」页顶上
    # 写着「逾期 12 条」，点进去一条没有，比不显示更让人不信任这个数。
    overdue = [t for t in tasks if t.is_overdue]
    open_ones = [t for t in tasks if t.status in OPEN_STATUS]
    today_due = [t for t in open_ones if t.due_date == date.today()]
    week_due = [t for t in open_ones if t.due_date
                and date.today() < t.due_date <= date.today() + timedelta(days=7)]

    by_user = {}
    for t in tasks:
        k = t.assignee or (t.assignee_user.display_name if t.assignee_user else "") or "未指派"
        by_user.setdefault(k, []).append(t)

    return render(request, "tasks/list.html", {
        "active": "tasks", "user": user, "tasks": tasks,
        "who": who, "status": status, "assignee": assignee, "q": q,
        "TASK_STATUS": TASK_STATUS,
        "open_count": len(open_ones),
        "overdue_count": len(overdue),
        "today_count": len(today_due),
        "week_count": len(week_due),
        "by_user": sorted(by_user.items(), key=lambda kv: (-len(kv[1]), kv[0])),
        "today": date.today(),
        "my_open": len([t for t in tasks
                        if t.status in OPEN_STATUS
                        and (t.assignee_user_id == user.id
                             or (t.assignee and t.assignee in
                                 {n for n in (user.display_name, user.username) if n}))]),
        # 负责人筛选下拉的候选。
        # staffs：名册里在职的（正常能选的人）
        # orphan_assignees：还在被任务引用、但名册里没有的历史名字——
        #   改版前负责人是自由文本，这些名字筛选时也得能选到，
        #   否则「按人查」对老数据完全失效。
        "staffs": db.query(Staff).order_by(Staff.sort_order, Staff.id).all(),
        "orphan_assignees": sorted(
            {t.assignee.strip() for t in tasks if t.assignee and t.assignee.strip()}
            - {s.name.strip() for s in db.query(Staff).all()}),
    })


@router.post("/{task_id}/set-status")
def task_status(task_id: int, request: Request, status: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    """改状态。在任务页改完立刻回任务页，不要跳回项目详情——那会让人以为白改了。

    路径叫 `set-status` 而不是 `status`：`projects.py` 里已有一个
    `POST /tasks/{id}/status`（同一个 URL，只是历史上挂在无前缀的 router 上）。
    两者路径完全撞车时，FastAPI 按注册顺序命中先注册的那个，
    而这里两个 router 都注册了——所以必须换名，不能靠顺序赌。
    """
    task = db.get(Task, task_id)
    if task is None:
        return RedirectResponse(url="/tasks", status_code=303)
    if status in TASK_STATUS and task.status != status:
        _old = task.status
        task.status = status
        db.commit()
        log_audit(db, user, "修改", "任务", task_id,
                  f"{task.title}：{_old} → {status}")
    back = request.headers.get("referer") or ""
    return RedirectResponse(url=back if "/tasks" in back else f"/projects/{task.project_id}#tasks",
                            status_code=303)


@router.post("/{task_id}/take")
def task_claim(task_id: int, request: Request,
               db: Session = Depends(get_db), user: User = Depends(login_required)):
    """认领：把自己绑成负责人。

    这条是为了让**历史任务**也能进「我的任务」——它们的 assignee_user_id
    是 NULL，只在 assignee 文本里写着某个名字。认领后写上外键，
    名字同时更新为当前账号的显示名。
    """
    task = db.get(Task, task_id)
    if task is None:
        return RedirectResponse(url="/tasks", status_code=303)
    name = (user.display_name or user.username or "").strip()
    task.assignee_user_id = user.id
    if name:
        task.assignee = name
    db.commit()
    log_audit(db, user, "认领", "任务", task_id, task.title)
    back = request.headers.get("referer") or ""
    return RedirectResponse(url=back if "/tasks" in back else "/tasks?who=mine",
                            status_code=303)
