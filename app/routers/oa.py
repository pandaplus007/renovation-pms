"""OA 用户、岗位与权限管理。账号/权限属高敏感模块，模块内动作由 oa_users 的动作位控制。"""
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import hash_password, login_required
from ..database import get_db
from ..models import AuditLog, Role, RoleModulePerm, User
from ..row_scope import SCOPE_HELP, SCOPE_LABELS, SCOPES, normalize
from ..permissions import (ACTIONS, ACTION_BITS, ACTION_LABELS, BUILTIN_ROLES,
                           MODULES, SUPER_ROLES, invalidate_perm_cache, is_superuser,
                           role_matrix)
from ..utils.audit import log_audit
from ..utils.files import IMAGE_EXTS, save_upload
from ..web import company_info, nav_labels, render, set_company, set_nav_labels

router = APIRouter(prefix="/oa", tags=["oa"])


def _roles(db: Session) -> list[Role]:
    """全部岗位（内置优先，按 sort_order）。表为空时回落到内置清单。"""
    rows = db.query(Role).order_by(Role.builtin.desc(), Role.sort_order, Role.id).all()
    if not rows:
        return [Role(name=n, builtin=True, sort_order=(i + 1) * 10) for i, n in enumerate(BUILTIN_ROLES)]
    return rows


def _notify_module():
    """取推送模块；**没部署**时返回 None。

    为什么要 optional：`app/notify.py` 不在所有部署里都存在。
    有两种部署形态：
      - 工作区/开发机：推送功能在，源码齐全
      - 精简生产部署：打包时排除 notify.py（当时决定「暂时不上线」）

    若写成硬导入 `from .. import notify`，精简部署会在**启动时**直接
    ImportError 崩掉——不是某个页面 500，是整个服务起不来，
    而且报错指向 import 循环之类的地方，看不出真正原因是「这个部署没这个功能」。
    所以这里显式 try/except，并让模板据此隐藏整个推送卡片
    （而不是显示一个点下去必然失败的按钮）。
    """
    try:
        from .. import notify
        return notify
    except ImportError:
        return None


@router.get("/settings")
def company_settings(request: Request, msg: str = "",
                     db: Session = Depends(get_db), user: User = Depends(login_required)):
    info = company_info()
    labels = nav_labels()
    notify = _notify_module()
    return render(request, "oa/settings.html", {
        "active": "oa_settings", "user": user,
        "company_name": info.get("name", "装修项目管家"),
        "company_logo": info.get("logo", ""),
        "nav_labels": labels,
        # 两个变量不是一回事：
        #   notify_deployed —— 功能代码在不在（决定渲染不渲染整张卡片）
        #   notify_enabled  —— 在且配了 webhook（决定卡片上的徽章文案）
        "notify_deployed": notify is not None,
        "notify_enabled": bool(notify and notify.enabled()),
        "msg": msg,
    })


@router.post("/settings/nav")
def settings_nav_save(request: Request,
                      labels: list[str] = Form([]),
                      keys: list[str] = Form([]),
                      db: Session = Depends(get_db),
                      user: User = Depends(login_required)):
    """保存导航文字自定义。"""
    data = {}
    for k, v in zip(keys, labels):
        if v and v.strip():
            data[k] = v.strip()
    set_nav_labels(data)
    return RedirectResponse(url="/oa/settings", status_code=303)


@router.post("/settings")
async def company_settings_save(request: Request, name: str = Form(""),
                                 file: UploadFile | None = File(None),
                                 db: Session = Depends(get_db),
                                 user: User = Depends(login_required)):
    logo = company_info().get("logo", "")
    if file and file.filename:
        path = await save_upload(file, "company", allowed=IMAGE_EXTS)
        if path:
            logo = path
    set_company(name=name, logo=logo)
    return RedirectResponse(url="/oa/settings", status_code=303)


@router.post("/settings/logo-remove")
def company_logo_remove(request: Request,
                        db: Session = Depends(get_db),
                        user: User = Depends(login_required)):
    set_company(name=company_info().get("name", "装修项目管家"), logo="")
    return RedirectResponse(url="/oa/settings", status_code=303)


@router.get("/notify-test")
def notify_test(request: Request,
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    """测试企微推送：真发一条到群，并回显结果。

    刻意做成 GET 链接而不是表单按钮：改配置时想验证，往往是已经在
    另一个窗口改好了 .env，回来点一下看结果。用 GET 才方便刷新重试。

    这里**真的发消息**（会产生一条群消息），但不发待办内容，
    免得测试时刷屏。返回 200/302 由前端 msg 参数展示。

    ⚠ 精简部署里本路由仍在（否则 `_route_actions.json` 的登记会与实际路由
    不一致，而 `test_all_routes.py` 是按源码枚举的），但推送模块不存在。
    所以这里必须先判 `notify is None`，否则会在未部署的实例上抛 ImportError，
    而这个 URL 在 OA 权限矩阵里是被允许访问的——即「有权限却 500」。
    """
    notify = _notify_module()
    if notify is None:
        return RedirectResponse(
            url="/oa/settings?msg=本部署未包含推送功能", status_code=303)
    if not notify.enabled():
        return RedirectResponse(
            url="/oa/settings?msg=推送未启用：请在 .env 配置 WECOM_WEBHOOK",
            status_code=303)
    ok, why = notify.test_connection()
    return RedirectResponse(
        url="/oa/settings?msg=" + (f"推送成功：{why}" if ok else f"推送失败：{why}"),
        status_code=303)


@router.get("/users")
def user_list(request: Request, q: str = "",
              db: Session = Depends(get_db), user: User = Depends(login_required)):
    query = db.query(User)
    if q:
        like = f"%{q}%"
        query = query.filter((User.username.like(like)) | (User.display_name.like(like)))
    users = query.order_by(User.role, User.id).all()
    return render(request, "oa/users.html", {
        "active": "oa_users", "user": user, "users": users, "q": q,
        "roles": _roles(db), "matrix": role_matrix(db),
        "view_counts": {
            rn: sum(1 for m in MODULES if mods.get(m, 0) & ACTION_BITS["view"])
            for rn, mods in role_matrix(db).items()
        },
    })


# ---------------------------------------------------------------- 权限矩阵

def _perm_ctx(db: Session, user: User, role: str = "", error: str = "") -> dict:
    """权限矩阵页上下文。role 为空时默认选中第一个非超级用户岗位。"""
    roles = _roles(db)
    matrix = role_matrix(db)
    if not role:
        role = next((r.name for r in roles if r.name not in SUPER_ROLES),
                    roles[0].name if roles else "")
    return {
        "active": "oa_users", "user": user, "roles": roles, "matrix": matrix,
        "modules": list(MODULES.values()),
        "actions": ACTIONS, "action_labels": ACTION_LABELS,
        "superuser": is_superuser(user),
        "current": role, "error": error,
        # 勾选状态在这里算好：Jinja2 没有位运算符，模板里写 flags & bit 会直接语法报错
        "checked": {
            m.key: {a: bool(matrix.get(role, {}).get(m.key, 0) & ACTION_BITS[a])
                    for a in ACTIONS}
            for m in MODULES.values()
        },
        "view_counts": {
            rn: sum(1 for m in MODULES if mods.get(m, 0) & ACTION_BITS["view"])
            for rn, mods in matrix.items()
        },
    }


@router.get("/permissions")
def permissions_page(request: Request, role: str = "",
                     db: Session = Depends(get_db), user: User = Depends(login_required)):
    return render(request, "oa/permissions.html", _perm_ctx(db, user, role))


@router.post("/permissions")
async def permissions_save(request: Request,
                           db: Session = Depends(get_db),
                           user: User = Depends(login_required)):
    """保存岗位权限矩阵。表单按 p_<module>_<action> 逐格提交。

    ⚠ 必须是 `async def`：只有 async 路由里 `await request.form()` 才可靠。
    写成同步 `def` 时 Starlette 在线程池里跑，`request.form()` 会去挂一个
    事件循环，拿到的不是 FormData 而是 **function 对象**——
    于是 `request.form.get(...)` 报
    「'function' object has no attribute 'get'」，整个保存页 500。
    这个错法很难联想到「async 与否」，注释留着。
    """
    form = await request.form()
    role = str(form.get("role", "")).strip()
    if not role or not db.query(Role).filter(Role.name == role).first():
        return RedirectResponse(url="/oa/permissions", status_code=303)
    if role in SUPER_ROLES:
        return render(request, "oa/permissions.html", _perm_ctx(
            db, user, role, f"「{role}」是超级用户岗位，恒定拥有全部权限，不可修改"))

    changes = []
    # 逐模块名遍历，不遍历 MODULES 的 value：
    # 原写法 `for mod in MODULES` 拿到的是 **key 字符串**，
    # 于是 `mod.key` 报「'str' object has no attribute 'key'」，
    # 整个权限保存页 500（历史上一直是坏的，只是没人用过这个路径）。
    for mod_key in MODULES:
        mod = MODULES[mod_key]
        flags = 0
        for act in ACTIONS:
            if str(form.get(f"p_{mod_key}_{act}", "")) == "1":
                flags |= ACTION_BITS[act]
        row = db.query(RoleModulePerm).filter(
            RoleModulePerm.role == role, RoleModulePerm.module == mod_key).first()
        if row is None:
            row = RoleModulePerm(role=role, module=mod_key, flags=0)
            db.add(row)
        if (row.flags or 0) != flags:
            changes.append(f"{mod.label}:{row.flags or 0}→{flags}")
            row.flags = flags
    db.commit()
    invalidate_perm_cache()
    log_audit(db, user, "修改", "岗位权限", None,
              f"{role} 权限更新（{len(changes)} 个模块变更）")
    return render(request, "oa/permissions.html", _perm_ctx(
        db, user, role, f"「{role}」权限已保存（{len(changes)} 个模块变更）"))


@router.post("/roles/new")
def role_create(request: Request, name: str = Form(...), note: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/oa/permissions", status_code=303)
    if db.query(Role).filter(Role.name == name).first():
        return render(request, "oa/permissions.html", _perm_ctx(
            db, user, "", f"岗位「{name}」已存在"))
    db.add(Role(name=name, builtin=False, sort_order=900, note=note.strip()))
    db.commit()
    invalidate_perm_cache()
    log_audit(db, user, "新增", "岗位", None, f"新建岗位 {name}")
    return RedirectResponse(url=f"/oa/permissions?role={name}", status_code=303)


@router.post("/roles/{role_id}/delete")
def role_delete(role_id: int, request: Request,
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    role = db.get(Role, role_id)
    if role is None or role.builtin:
        return RedirectResponse(url="/oa/permissions", status_code=303)
    if db.query(User).filter(User.role == role.name).first():
        return render(request, "oa/permissions.html", _perm_ctx(
            db, user, role.name, f"岗位「{role.name}」下仍有账号，请先调整这些账号的岗位"))
    name = role.name
    db.query(RoleModulePerm).filter(RoleModulePerm.role == name).delete()
    db.delete(role)
    db.commit()
    invalidate_perm_cache()
    log_audit(db, user, "删除", "岗位", None, f"删除岗位 {name}")
    return RedirectResponse(url="/oa/permissions", status_code=303)


@router.get("/users/new")
def user_new(request: Request, user: User = Depends(login_required)):
    return render(request, "oa/user_form.html", {
        "active": "oa_users", "user": user, "target": None,
    })


@router.post("/users/new")
def user_create(request: Request,
                username: str = Form(...), display_name: str = Form(""),
                password: str = Form(...), role: str = Form("员工"),
                data_scope: str = Form("all"),
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    username = username.strip()
    role = role.strip() or "员工"
    err = ""
    if not username:
        err = "用户名不能为空"
    elif db.query(User).filter(User.username == username).first():
        err = f"用户名「{username}」已存在"
    elif not db.query(Role).filter(Role.name == role).first() and role not in SUPER_ROLES:
        err = f"岗位「{role}」不存在"
    elif len(password) < 6:
        err = "密码至少 6 位"
    if err:
        return render(request, "oa/user_form.html", {
            "active": "oa_users", "user": user, "target": None,
            "error": err, "roles": _roles(db),
            "SCOPES": SCOPES, "SCOPE_LABELS": SCOPE_LABELS, "SCOPE_HELP": SCOPE_HELP,
        })
    db.add(User(username=username, display_name=display_name.strip(),
                password_hash=hash_password(password), role=role, is_active=True,
                data_scope=normalize(data_scope)))
    db.commit()
    log_audit(db, user, "新增", "账号", None, f"新建账号 {username}（{role}）")
    return RedirectResponse(url="/oa/users", status_code=303)


@router.get("/users/{user_id}/edit")
def user_edit(user_id: int, request: Request,
              db: Session = Depends(get_db), user: User = Depends(login_required)):
    target = db.get(User, user_id)
    if target is None:
        return RedirectResponse(url="/oa/users", status_code=303)
    return render(request, "oa/user_form.html", {
        "active": "oa_users", "user": user, "target": target, "roles": _roles(db),
        "SCOPES": SCOPES, "SCOPE_LABELS": SCOPE_LABELS, "SCOPE_HELP": SCOPE_HELP,
    })


def _last_superuser(db: Session, exclude_id: int | None = None) -> bool:
    """除 exclude 外是否已没有其它超级用户——用于防止把最后一个管理员降级/停用导致锁死。"""
    q = db.query(User).filter(User.role.in_(list(SUPER_ROLES)), User.is_active.is_(True))
    if exclude_id is not None:
        q = q.filter(User.id != exclude_id)
    return q.count() == 0


@router.post("/users/{user_id}/edit")
def user_update(user_id: int, request: Request,
                display_name: str = Form(""), role: str = Form("员工"),
                is_active: int = Form(1), password: str = Form(""),
                data_scope: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    target = db.get(User, user_id)
    if target is None:
        return RedirectResponse(url="/oa/users", status_code=303)
    role = role.strip() or "员工"

    def _err(msg):
        return render(request, "oa/user_form.html", {
            "active": "oa_users", "user": user, "target": target,
            "error": msg, "roles": _roles(db),
            "SCOPES": SCOPES, "SCOPE_LABELS": SCOPE_LABELS, "SCOPE_HELP": SCOPE_HELP,
        })

    if not db.query(Role).filter(Role.name == role).first() and role not in SUPER_ROLES:
        return _err(f"岗位「{role}」不存在")
    if password and len(password) < 6:
        return _err("密码至少 6 位")
    # 防锁死：不能把最后一个可用的超级用户降级或停用
    was_super = target.role in SUPER_ROLES
    now_active = bool(is_active)
    if was_super and (role not in SUPER_ROLES or not now_active):
        if _last_superuser(db, exclude_id=target.id):
            return _err("系统必须保留至少一个启用的超级用户（老板），无法降级或停用该账号")

    changes = []
    if target.display_name != display_name.strip():
        changes.append(f"姓名→{display_name.strip()}")
    if target.role != role:
        changes.append(f"岗位 {target.role}→{role}")
    if target.is_active != now_active:
        changes.append("启用" if now_active else "停用")
    # 范围留空 = 不改。空提交不该把范围重置成 all：
    # 有人在别处改了表单再回来时，空值会悄悄收回别人刚设的权限。
    _new_scope = None
    if data_scope:
        _new_scope = normalize(data_scope)
        if _new_scope != (target.data_scope or "all"):
            changes.append(f"数据范围→{SCOPE_LABELS.get(_new_scope, _new_scope)}")
    if password:
        changes.append("重置密码")

    target.display_name = display_name.strip()
    target.role = role
    target.is_active = now_active
    if _new_scope is not None:
        target.data_scope = _new_scope
    if password:
        target.password_hash = hash_password(password)
    db.commit()
    # 改密码后强制该账号重新登录（旧会话里可能还留着旧权限）
    if password:
        invalidate_sessions(target)
    log_audit(db, user, "修改", "账号", target.id,
              f"{target.username}：" + ("；".join(changes) or "无变化"))
    return RedirectResponse(url="/oa/users", status_code=303)


def invalidate_sessions(target: User) -> None:
    """密码被重置后递增会话版本号，使该账号已签发的会话在下一次请求时失效。"""
    from ..auth import SESSION_VERSION_FIELD
    setattr(target, SESSION_VERSION_FIELD,
            (getattr(target, SESSION_VERSION_FIELD, 1) or 1) + 1)
    from ..database import SessionLocal
    s = SessionLocal()
    try:
        s.commit()
    finally:
        s.close()


@router.post("/users/{user_id}/delete")
def user_delete(user_id: int, request: Request,
                db: Session = Depends(get_db), user: User = Depends(login_required)):
    target = db.get(User, user_id)
    if target is None:
        return RedirectResponse(url="/oa/users", status_code=303)

    def _err(msg):
        return render(request, "oa/users.html", {
            "active": "oa_users", "user": user,
            "users": db.query(User).order_by(User.role, User.id).all(),
            "q": "", "roles": _roles(db), "matrix": role_matrix(db),
            "view_counts": {
                rn: sum(1 for m in MODULES if mods.get(m, 0) & ACTION_BITS["view"])
                for rn, mods in role_matrix(db).items()
            },
            "error": msg,
        })

    if target.id == user.id:
        return _err("不能删除当前登录账号")
    if target.role in SUPER_ROLES and _last_superuser(db, exclude_id=target.id):
        return _err("系统必须保留至少一个启用的超级用户（老板）")
    name = target.username
    db.delete(target)
    db.commit()
    log_audit(db, user, "删除", "账号", user_id, f"删除账号 {name}")
    return RedirectResponse(url="/oa/users", status_code=303)

@router.get("/audit")
def audit_logs(request: Request, action: str = "", entity_type: str = "",
               username: str = "", limit: int = 200,
               db: Session = Depends(get_db), user: User = Depends(login_required)):
    """操作审计日志（仅老板/admin 可访问，由权限守卫限制 /oa）。"""
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if username:
        query = query.filter(AuditLog.username.like(f"%{username}%"))
    logs = query.order_by(AuditLog.id.desc()).limit(limit).all()
    types = [r[0] for r in db.query(AuditLog.entity_type).distinct().all() if r[0]]
    return render(request, "oa/audit.html", {
        "active": "oa", "user": user, "logs": logs,
        "action": action, "entity_type": entity_type, "username": username,
        "types": types, "limit": limit,
    })
