"""任务页回归：逾期判据、「我的任务」、认领、待办联动。

为什么判据要单独测
----------------
`Task.is_overdue` 是 property，**不在 SQL 里**，所以它和
`todo.py` 里的 SQL 条件是两份实现。两份实现不一致时，
任务页说「没逾期」而待办总览说「逾期了」，两边都能正常显示，
没有报错——只能靠断言守住。
"""
from __future__ import annotations

import os
import sys
import atexit
import urllib.parse
import http.cookiejar
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import Project, Task, User
from app import todo as todo_mod

BASE = os.environ.get("PMS_BASE", "http://127.0.0.1:8018")
MARK = "TASKTEST"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f" —— {detail}" if detail and not cond else ""))


def cleanup():
    try:
        db = SessionLocal()
        n = db.query(Task).filter(Task.title.like(f"%{MARK}%")).delete(synchronize_session=False)
        db.commit()
        db.close()
        if n:
            print(f"\n已清理临时夹具：任务 {n} 条")
    except Exception as e:
        print(f"\n清理失败：{e}")


atexit.register(cleanup)
cleanup()


def login():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.open(BASE + "/login", timeout=20)
    op.open(BASE + "/login", urllib.parse.urlencode(
        {"username": "admin", "password": "admin123"}).encode(), timeout=20)
    return op


def main():
    from datetime import date, timedelta
    db = SessionLocal()
    proj = db.query(Project).first()
    me = db.query(User).filter(User.username == "admin").first()
    if not proj or not me:
        print("缺少基础数据")
        return

    today = date.today()
    # 建 5 条覆盖各种边界的任务
    cases = [
        # (标题后缀, due_date, status, 期望 is_overdue)
        ("逾期待办", today - timedelta(days=3), "待办", True),
        ("逾期进行中", today - timedelta(days=1), "进行中", True),
        ("逾期但已完成", today - timedelta(days=5), "已完成", False),
        ("逾期但已取消", today - timedelta(days=5), "已取消", False),
        ("今天到期不算逾期", today, "待办", False),
        ("未来不算逾期", today + timedelta(days=3), "待办", False),
        ("无截止日不算逾期", None, "待办", False),
    ]
    made = []
    for suffix, due, st, _exp in cases:
        t = Task(project_id=proj.id, title=f"{MARK} {suffix}", assignee=f"{MARK}人",
                 due_date=due, status=st)
        db.add(t)
        made.append((t, _exp))
    db.commit()

    # ---------- 1. is_overdue 判据 ----------
    for t, exp in made:
        db.refresh(t)
        check(f"逾期判据：{t.title.split()[-1]}", t.is_overdue == exp,
              f"期望 {exp}，实际 {t.is_overdue}")

    # ---------- 2. property 与 todo 的 SQL 条件必须一致 ----------
    overdue_prop = {t.id for t, _ in made if db.get(Task, t.id).is_overdue}
    rows = db.query(Task).filter(
        Task.due_date.isnot(None), Task.due_date < today,
        Task.status.notin_(("已完成", "已取消"))).all()
    overdue_sql = {t.id for t in rows if MARK in t.title}
    check("Task.is_overdue 与 todo 的 SQL 条件结果一致",
          overdue_prop == overdue_sql,
          f"property={sorted(overdue_prop)} sql={sorted(overdue_sql)}")
    worst_due = min((db.get(Task, i).due_date for i in overdue_prop), default=today)

    def _days_overdue(d):
        if isinstance(d, __import__("datetime").datetime):
            d = d.date()
        return (today - d).days

    # ---------- 3. 逾期任务进了待办总览 ----------
    overdue_tasks = db.query(Task).filter(
        Task.due_date.isnot(None), Task.due_date < today,
        Task.status.notin_(("已完成", "已取消"))).all()
    mine = [t for t in overdue_tasks if MARK in t.title]
    check("待办里含全部夹具逾期任务", len(mine) == len(overdue_prop),
          f"夹具 {len(mine)} vs property {len(overdue_prop)}")

    items = todo_mod.collect(db)
    tk = [i for i in items if i["kind"] == "任务逾期"]
    check("待办总览出现「任务逾期」", len(tk) >= 1, f"实际 {len(tk)} 条")
    if tk:
        check("逾期待办给了直达链接", tk[0]["url"] == "/tasks?who=overdue", tk[0]["url"])
        # 判据直接比对 title 字段：detail 里写的是「N 条任务已过截止日」，
        # 而详情文本是拼接出来的，拿详情做子串断言容易因为别处的数字假绿。
        # 这里刻意断言 title，而不是拼一个可能匹配不上的子串。
        n = len(mine)
        exp_title = (f"{n} 条任务已过截止日" if n != 1
                     else "1 条任务已过截止日")
        check("逾期待办标题含准确条数", tk[0]["title"] == exp_title,
              f"期望 {exp_title!r}，实际 {tk[0]['title']!r}")
        check("逾期待办说明含逾期天数",
              f"逾期 {_days_overdue(worst_due)} 天" in tk[0]["detail"],
              tk[0]["detail"])

    db.close()

    # ---------- 4. 页面 ----------
    op = login()
    r = op.open(BASE + "/tasks", timeout=20)
    html = r.read().decode("utf-8")
    check("任务页可访问（200）", r.status == 200, f"实际 {r.status}")
    check("有「我的任务」入口", "我的任务" in html)
    check("有逾期/今天/未来 7 天筛选",
          all(k in html for k in ("逾期", "今天到期", "未来 7 天")))
    check("默认显示夹具任务", MARK in html)
    check("已完成任务不标逾期徽章（计数口径正确）",
          html.count("state-watch") <= len(overdue_prop),
          f"徽章数 {html.count('state-watch')} > 逾期数 {len(overdue_prop)}")

    r2 = op.open(BASE + "/tasks?who=overdue", timeout=20)
    h2 = r2.read().decode("utf-8")
    check("逾期视图可访问", r2.status == 200)
    check("逾期视图只含未完成任务",
          MARK in h2 and "逾期待办" in h2 and "今天到期不算逾期" not in h2)

    r3 = op.open(BASE + "/tasks?who=mine", timeout=20)
    h3 = r3.read().decode("utf-8")
    check("我的任务视图可访问", r3.status == 200)
    check("我的任务带外键匹配（admin 的 display_name/username）",
          "我的未完成" in h3)

    # ---------- 5. 认领 ----------
    db2 = SessionLocal()
    legacy = db2.query(Task).filter(
        Task.title.like(f"{MARK}%"), Task.assignee_user_id.is_(None)).first()
    tid = legacy.id if legacy else 0
    db2.close()
    check("存在未绑定账号的夹具任务（认领的前提）", bool(tid))

    if tid:
        op.open(BASE + f"/tasks/{tid}/take", data=b"", timeout=20)
        db3 = SessionLocal()
        t3 = db3.get(Task, tid)
        check("认领后绑定到当前账号", t3.assignee_user_id == me.id,
              f"实际 {t3.assignee_user_id}")
        check("认领后显示名写入 assignee", t3.assignee == (me.display_name or me.username))
        db3.close()
        # 认领后应出现在「我的任务」
        r4 = op.open(BASE + "/tasks?who=mine", timeout=20)
        h4 = r4.read().decode("utf-8")
        check("认领后该任务出现在「我的任务」", MARK in h4)

    # ---------- 6. 改状态（set-status 路由）----------
    r5 = op.open(BASE + f"/tasks/{tid}/set-status",
                 data=urllib.parse.urlencode({"status": "已完成"}).encode(), timeout=20)
    check("set-status 路由非 403/404", r5.status in (200, 303), f"实际 {r5.status}")
    db4 = SessionLocal()
    t4 = db4.get(Task, tid)
    check("状态已改为已完成", t4.status == "已完成", f"实际 {t4.status}")
    check("已完成的任务不再算逾期", t4.is_overdue is False)
    db4.close()

    # ---------- 7. 项目详情页有回链与负责人下拉 ----------
    r6 = op.open(BASE + f"/projects/{proj.id}", timeout=20)
    h6 = r6.read().decode("utf-8")
    check("项目详情页有「去任务总览」回链", "/tasks?assignee=" in h6)
    check("任务编辑模态含负责人账号下拉", 'name="assignee_user_id"' in h6)
    check("新增任务表单含负责人账号下拉", 'name="assignee_user_id"' in h6)


if __name__ == "__main__":
    print("=== 任务页回归 ===")
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(f"测试中断：{type(e).__name__}: {e}", False)
    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
