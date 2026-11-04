"""FastAPI 主应用入口。"""
import os
import sys

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import verify_password
from .config import SECRET_KEY, SESSION_COOKIE, SESSION_MAX_AGE, UPLOAD_DIR
from .database import get_db, report_sqlite_mode
from .models import User
from .permissions import init_route_index, role_guard
from .routers import (api, approvals, changes, company_docs, contracts, customers,
                      dashboard, expenses, inventory, invoices, materials, oa,
                      payables, phases, portal, projects, purchases, quotes,
                      reconcile, reports, sales_invoices, screen, search,
                      staff, suppliers, supplier_payments, tasks)
from .seed import seed
from .web import STATIC_DIR, render

import threading
import time
from .database import backup_db


def _start_backup_loop():
    """启动时备份一次 + 每日自动备份（后台守护线程）。"""
    try:
        backup_db()
    except Exception:
        pass
    t = threading.Thread(target=_backup_loop_worker, daemon=True)
    t.start()


def _backup_loop_worker():
    while True:
        time.sleep(86400)
        try:
            backup_db()
        except Exception:
            pass


app = FastAPI(title="装修项目管理系统", docs_url=None, redoc_url=None,
              openapi_url=None,
              dependencies=[Depends(role_guard)])

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie=SESSION_COOKIE,
    max_age=SESSION_MAX_AGE,
    same_site="lax",
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 上传文件静态服务（图片/文档）
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# 首次启动自动建表 + 演示数据
seed()


# 全局权限守卫：已登录用户无模块权限时返回 403 页面
@app.exception_handler(403)
async def forbidden_handler(request: Request, exc):
    user = getattr(request.state, "user", None)
    return render(request, "403.html", {"user": user, "active": ""}, status_code=403)


# 数据库写锁超时 → 可读的 503，而不是白页 500。
#
# busy_timeout 已经自动排队 30 秒，能走到这里说明是真冲突（多人同时提交，
# 或数据库在不支持 WAL 的网络盘上）。默认行为是 500 + 堆栈，员工看不懂，
# 更麻烦的是「不知道刚才那条有没有存进去」——于是不敢重试，改回微信群。
# 这里明确告诉他「没生效也没丢数据」，把重试变成一个安全的动作。
@app.exception_handler(OperationalError)
async def db_locked_handler(request: Request, exc: OperationalError):
    msg = str(exc).lower()
    if "locked" not in msg and "busy" not in msg:
        # 不是锁的问题就老实走 500，别把它伪装成「系统忙」——
        # 真正的 SQL 错误需要被看见，而不是被一个友好页面吃掉。
        raise exc
    user = getattr(request.state, "user", None)
    return render(request, "db_busy.html",
                  {"user": user, "active": ""}, status_code=503)


# ---------- 登录 / 退出 ----------
@app.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=303)
    return render(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 db: Session = Depends(get_db)):
    from .auth import (check_login_locked, clear_login_failures, establish_session,
                       needs_rehash, note_login_failure, hash_password)
    from .utils.audit import log_audit

    wait = check_login_locked(request, username)
    if wait:
        return render(request, "login.html", {
            "error": f"登录失败次数过多，请 {wait // 60 + 1} 分钟后再试",
        }, status_code=429)

    user = db.query(User).filter(User.username == username).first()
    if user and user.is_active and verify_password(password, user.password_hash):
        clear_login_failures(request, username)
        # 老哈希（单轮 SHA-256）在登录成功时透明升级为 PBKDF2
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
            db.commit()
        establish_session(request, user)
        return RedirectResponse(url="/", status_code=303)

    note_login_failure(request, username)
    log_audit(db, None, "登录失败", "登录", None, f"用户名 {username}")
    return render(request, "login.html", {"error": "用户名或密码错误"}, status_code=400)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ---------- 业务路由 ----------
app.include_router(dashboard.router)
app.include_router(customers.router)
app.include_router(projects.router)
app.include_router(materials.router)
app.include_router(purchases.router)
app.include_router(inventory.router)
app.include_router(suppliers.router)
app.include_router(reports.router)
app.include_router(invoices.router)
app.include_router(supplier_payments.router)
app.include_router(api.router)
app.include_router(contracts.router)
from .routers import subcontracts
app.include_router(subcontracts.router)
app.include_router(expenses.router)
app.include_router(company_docs.router)
app.include_router(oa.router)
app.include_router(staff.router)
app.include_router(phases.router)
app.include_router(tasks.router)
app.include_router(portal.router)
app.include_router(search.router)
app.include_router(approvals.router)
app.include_router(reconcile.router)
app.include_router(payables.router)
app.include_router(changes.router)
app.include_router(quotes.router)
app.include_router(sales_invoices.router)
app.include_router(screen.router)

# 路由全部注册完毕后建立 路由 → (模块, 动作) 索引，供权限守卫做 fail-closed 判定
init_route_index(app)

# 权限播种放在此处而非 database 模块内：permissions 依赖 auth 依赖 database，
# 在 database 导入期调用会形成循环导入
from .database import _seed_permissions  # noqa: E402

_seed_permissions()

_start_backup_loop()

# 打印**读回**的日志模式，而不是「设置过了」的自我确认。
# WAL 在不支持的文件系统上静默失败（设了不报错也不生效），不看这行就只能靠猜。
print(f"[db] 日志模式：{report_sqlite_mode()}", file=sys.stderr)
