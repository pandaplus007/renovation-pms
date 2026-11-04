"""公司人员名册：维护「人」，不是「账号」。

区分两件事（这个模块存在的全部理由）：
  · **人**（本模块）：工长、泥瓦师傅、水电师傅、设计师、监理……
    他们要能被指派任务、被按人筛选，但绝大多数**不登录系统**。
  · **账号**（OA → 用户）：能登录、有权限、有岗位的人。

改版前两者混为一谈，导致两处都不可用：
  · 负责人下拉挂 users，而下拉里只有 admin/boss 两个老板（实测生产库），
    工长一个都选不出来；
  · 于是负责人只能自由文本，同一个人的写法分裂成「刘工」「刘师傅」「老刘」，
    按人筛选和统计全部失效。

本模块只做名册的增删改查。人员与任务的关联仍然走既有字段
（Task.assignee / GanttTask.assignee / Project.designer|foreman|supervisor），
存的是**姓名文本**——不改成外键，是因为历史数据已经是姓名文本，
改成外键等于要迁移全部存量记录，而收益只是「改名能自动同步」。
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import GanttTask, Project, Staff, Task, User
from ..web import render

router = APIRouter(prefix="/staff", tags=["staff"])

# 工种预设。只是输入提示，不限制——用 datalist 而不是 select，
# 因为「工种」这个词在装修公司里没有标准答案（有的叫「泥工」有的叫「泥瓦」）。
JOBS = ["工长", "项目经理", "设计师", "监理", "水电", "泥瓦", "木工",
        "油漆", "安装", "拆除", "普工", "客服", "采购", "财务"]


def _usage(db: Session) -> dict[str, int]:
    """统计每个姓名被多少条记录引用。

    名册页要显示「刘工 在 5 个任务里」，这样用户才知道删掉这个人会有什么后果。
    按姓名文本统计（而不是外键），因为这些字段存的就是文本。
    """
    counts: dict[str, int] = {}

    def bump(name):
        n = (name or "").strip()
        if n:
            counts[n] = counts.get(n, 0) + 1

    for (a,) in db.query(Task.assignee).all():
        bump(a)
    for (a,) in db.query(GanttTask.assignee).all():
        bump(a)
    for d, f, s in db.query(Project.designer, Project.foreman,
                            Project.supervisor).all():
        bump(d)
        bump(f)
        bump(s)
    return counts


@router.get("")
def staff_list(request: Request, msg: str = "", q: str = "",
               show: str = "", db: Session = Depends(get_db),
               user: User = Depends(login_required)):
    query = db.query(Staff)
    if q:
        like = f"%{q}%"
        query = query.filter((Staff.name.like(like)) | (Staff.job.like(like))
                             | (Staff.phone.like(like)))
    # 默认只显示在职的：离职的人还留在库里（历史任务引用着他们的名字），
    # 但日常维护名册时不该被他们干扰。查看全部要点「含已停用」。
    if show != "all":
        query = query.filter(Staff.active.is_(True))
    staff = query.order_by(Staff.sort_order, Staff.id).all()

    usage = _usage(db)
    rows = [{"s": s, "used": usage.get(s.name.strip(), 0)} for s in staff]

    # 名册之外、但存量数据里还在用的名字。
    # 这是「数据一致性告警」：说明有人绕过名册写进去了（老数据或导入）。
    # 不自动补录——自动补录会让名册变成一个什么都往里塞的垃圾桶。
    known = {s.name.strip() for s in db.query(Staff).all()}
    orphans = sorted(n for n in usage if n not in known)

    active_count = db.query(Staff).filter(Staff.active.is_(True)).count()
    all_count = db.query(Staff).count()

    return render(request, "staff/list.html", {
        "active": "staff", "user": user,
        "rows": rows, "msg": msg, "q": q, "show": show,
        "jobs": JOBS, "orphans": orphans,
        "active_count": active_count, "all_count": all_count,
        # 「绑定登录账号」下拉的候选。不放进全局 OPTIONS：那里是静态常量表，
        # 塞一个数据库查询会让每个页面都多一次查库。
        "users": (db.query(User).filter(User.is_active.is_(True))
                    .order_by(User.id).all()),
    })


@router.post("")
def staff_create(request: Request, name: str = Form(...),
                 job: str = Form(""), phone: str = Form(""),
                 note: str = Form(""), sort_order: int = Form(100),
                 user_id: int = Form(0),
                 db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(url="/staff?msg=姓名不能为空", status_code=303)

    # 同名拦截。名册里出现两个「刘工」，负责人下拉就变成猜谜。
    # 用姓名精确匹配（strip 后），因为负责人字段存的就是姓名文本——
    # 允许重名的话，按人筛选立刻失效。
    dup = db.query(Staff).filter(Staff.name == nm).first()
    if dup:
        tail = "" if dup.active else "（该名字已停用，可在「含已停用」里重新启用）"
        return RedirectResponse(
            url=f"/staff?msg=名册里已经有「{nm}」了{tail}", status_code=303)

    db.add(Staff(name=nm, job=job.strip(), phone=phone.strip(),
                 note=note.strip(), sort_order=sort_order,
                 user_id=user_id or None))
    db.commit()
    return RedirectResponse(url="/staff?msg=已添加", status_code=303)


@router.post("/{staff_id}/edit")
def staff_edit(staff_id: int, request: Request, name: str = Form(...),
               job: str = Form(""), phone: str = Form(""),
               note: str = Form(""), sort_order: int = Form(100),
               user_id: int = Form(0),
               db: Session = Depends(get_db),
               user: User = Depends(login_required)):
    s = db.get(Staff, staff_id)
    if not s:
        return RedirectResponse(url="/staff?msg=找不到该人员", status_code=303)

    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(url="/staff?msg=姓名不能为空", status_code=303)

    dup = (db.query(Staff)
             .filter(Staff.name == nm, Staff.id != staff_id).first())
    if dup:
        return RedirectResponse(
            url=f"/staff?msg=名册里已经有「{nm}」了", status_code=303)

    old_name = s.name
    s.name = nm
    s.job = job.strip()
    s.phone = phone.strip()
    s.note = note.strip()
    s.sort_order = sort_order
    s.user_id = user_id or None

    # 改名要顺带同步存量记录里的姓名文本。
    #
    # 不同步的话，「刘工」改成「刘师傅」之后，那 5 个任务还写着「刘工」，
    # 而名册里已经没有这个人了——名册页会立刻跳出一堆「名册外的人」告警，
    # 用户以为出了 bug。存储是文本，没有外键级联，只能在这里手动追。
    renamed = 0
    if old_name != nm:
        for t in db.query(Task).filter(Task.assignee == old_name).all():
            t.assignee = nm
            renamed += 1
        for g in db.query(GanttTask).filter(GanttTask.assignee == old_name).all():
            g.assignee = nm
            renamed += 1
        for col in (Project.designer, Project.foreman, Project.supervisor):
            for p in db.query(Project).filter(col == old_name).all():
                setattr(p, col.key, nm)
                renamed += 1

    db.commit()
    tail = f"，并同步了 {renamed} 处引用" if renamed else ""
    return RedirectResponse(url=f"/staff?msg=已保存{tail}", status_code=303)


@router.post("/{staff_id}/toggle")
def staff_toggle(staff_id: int, request: Request,
                 db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    """在职 / 离职切换。刻意不叫「删除」——离职的人历史任务里还有他。"""
    s = db.get(Staff, staff_id)
    if s:
        s.active = not s.active
        db.commit()
    return RedirectResponse(url="/staff", status_code=303)


@router.post("/{staff_id}/delete")
def staff_delete(staff_id: int, request: Request,
                 db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    """彻底删除。只在没有任何记录引用时才允许。

    引用检查不是「怕外键报错」（这里根本没有外键），而是：
    删掉「刘工」之后，那 5 个任务的负责人就变成一个查不到的名字——
    名册页跳「名册外的人」告警，而用户完全不知道该怎么修。
    与其这样，不如拦住，让他先改任务或改成停用。
    """
    s = db.get(Staff, staff_id)
    if not s:
        return RedirectResponse(url="/staff?msg=找不到该人员", status_code=303)

    used = _usage(db).get(s.name.strip(), 0)
    if used > 0:
        return RedirectResponse(
            url=f"/staff?msg=「{s.name}」还被 {used} 处记录引用，"
                f"不能删除。请改用「停用」——离职后历史任务仍需要这个名字。",
            status_code=303)

    db.delete(s)
    db.commit()
    return RedirectResponse(url="/staff?msg=已删除", status_code=303)
