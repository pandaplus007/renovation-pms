"""业主只读链接：公开页（免登录）+ 项目侧管理。

两个前缀，权限模型完全不同，别混：

- `/p/<token>`                —— **免登录**。业主看到的页面，凭 token 进。
- `/projects/{id}/portal*`    —— 项目详情里的管理页，需登录 + 模块权限 + 行级权限。

⚠ `/p/` 必须登记进 `PUBLIC_PREFIXES`（permissions.py），否则全局守卫
fail-closed 会把业主的请求判成 403——症状是「业主说打不开链接」，
而服务端日志里只有一条无权限，看不出是路由没放行。

⚠ 公开页**不走 web.render()**：那个函数会注入导航、模块注册表和侧栏项目树，
   业主没账号，全是 403 死链，还等于把系统模块结构公开了。
   这里直接用 templates.TemplateResponse，只给页面自己需要的几个字段。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import Project, ProjectPortalLink, User
from ..portal import (build_view, ensure_link, find_by_token, get_link, new_token,
                      touch)
from ..row_scope import can_see_project
from ..utils.audit import log_audit
from ..web import company_info, render, templates

router = APIRouter(tags=["portal"])

# 公开页的缓存策略：业主常常在手机浏览器 / 微信内置浏览器里打开，
# 这些浏览器缓存很激进。停用链接后如果还能从缓存里看到内容，
# 「停用」这个动作就是假的——业主那边完全没感知。
NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
            "X-Robots-Tag": "noindex, nofollow"}


# ================================================================== 业主侧（免登录）

@router.get("/p/{token}")
def portal_view(token: str, request: Request, db: Session = Depends(get_db)):
    """业主看到的页面。凭 token 进，不登录。

    失败一律返回**同一个页面** + 同样的话术：
    - 状态码 404，而不是 403 或 401；
    - 不区分「不存在」「已停用」「已过期」三种原因——分开了就成了枚举工具，
      能拿来试哪些 token 曾经有效，也顺便确认了「这个项目存在过」。

    `touch()` 放在渲染前，这样「访问次数」计的是**请求数**而不是「渲染成功数」：
    如果渲染时抛异常，这次访问在统计上仍然发生过——排查「业主说打不开」
    时这两者的差别很重要。
    """
    project = find_by_token(db, token)
    if project is None:
        return templates.TemplateResponse(
            request, "portal/invalid.html",
            {"company_name": company_info().get("name", "")},
            status_code=404, headers=NO_STORE)

    link = (db.query(ProjectPortalLink)
            .filter(ProjectPortalLink.project_id == project.id,
                    ProjectPortalLink.enabled.is_(True)).first())
    # find_by_token 已经校验过 enabled 与过期，这里再查一次是为了拿到
    # show_photos / show_payment 两个开关（它们不在返回值里）。
    view = build_view(db, project, link)
    touch(db, project)

    info = company_info()
    return templates.TemplateResponse(
        request, "portal/view.html",
        {"v": view,
         "company_name": info.get("name", ""),
         "company_logo": info.get("logo", ""),
         "today": date.today()},
        headers=NO_STORE)


# ================================================================== 项目侧（需登录）

def _load_project(project_id: int, db: Session, user) -> Project:
    """取项目并做行级权限校验。

    越权一律 404 而不是 403，与项目详情页同一判据——403 会泄露
    「这个项目存在」，而拿到的往往只是一个被老板猜到的 id。
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if not can_see_project(user, project, Project):
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.get("/projects/{project_id}/portal")
def portal_manage(project_id: int, request: Request,
                  db: Session = Depends(get_db),
                  user: User = Depends(login_required)):
    """管理页。注意用 `get_link` 而不是 `ensure_link`：
    ensure_link 会把三个开关写成传入值，在 GET 里调它等于**每次打开页面
    就把业主手上的链接重置掉**，而且零报错。"""
    project = _load_project(project_id, db, user)
    link = get_link(db, project_id)
    base = _base_url(request)
    return render(request, "portal/manage.html", {
        "project": project,
        "link": link,
        "full_url": f"{base}/p/{link.token}" if link.enabled else "",
        "base_url": base,
        "active": "projects",
        "user": user,
        "saved": request.query_params.get("saved", ""),
        "localhost_only": base.startswith("http://127.0.0.1")
                           or base.startswith("http://localhost"),
        # 占位符单独提示：`.env` 里抄了示例地址时，localhost 警告不亮，
        # 但链接依然是错的，必须有一句直接说「你抄的是占位符」。
        "portal_url_is_placeholder": _portal_url_is_placeholder(),
    })


@router.post("/projects/{project_id}/portal")
def portal_save(project_id: int, request: Request,
                enabled: str = Form(""), show_photos: str = Form(""),
                show_payment: str = Form(""), expire_days: str = Form("180"),
                db: Session = Depends(get_db),
                user: User = Depends(login_required)):
    project = _load_project(project_id, db, user)

    try:
        days = int(expire_days) if str(expire_days).strip() else 180
    except ValueError:
        days = 180
    # 上限 3 年。再长就等于「永久有效」，而这是个公网上的入口。
    days = max(1, min(days, 1095))

    link, _created = ensure_link(
        db, project,
        enabled=bool(enabled),
        show_photos=bool(show_photos),
        show_payment=bool(show_payment),
        expires_days=days,
    )
    log_audit(db, user,
              "启用" if link.enabled else "停用", "业主链接", project.id,
              f"{project.name}：链接{'已启用' if link.enabled else '已停用'}"
              f"（照片={'开' if link.show_photos else '关'}，"
              f"收款={'开' if link.show_payment else '关'}，{days} 天）")
    return RedirectResponse(
        url=f"/projects/{project_id}/portal?saved=1", status_code=303)


@router.post("/projects/{project_id}/portal/reset")
def portal_reset(project_id: int, request: Request,
                 db: Session = Depends(get_db),
                 user: User = Depends(login_required)):
    """换 token（旧链接立刻失效）。

    用途：链接发错人、或怀疑被转发给别人时。
    只换 token，不动 enabled 与两个开关——换链接的理由是「发错对象」，
    不是「设置要改」，把设置一起清了会让用户再点一次保存。
    """
    project = _load_project(project_id, db, user)
    row = get_link(db, project_id)
    old = row.token
    row.token = new_token()
    db.commit()
    log_audit(db, user, "修改", "业主链接", project.id,
              f"{project.name}：更换链接地址，旧链接立即失效")
    return RedirectResponse(
        url=f"/projects/{project_id}/portal?saved=reset", status_code=303)


# 配置里出现这些片段，说明**照着文档把占位符抄进去了**，没换成真地址。
# 例如本文件顶部注释里的示例 `http://你的对外地址:8018`。
_PLACEHOLDER_MARKS = ("你的", "请填", "示例", "your-domain", "example.com",
                      "example.org", "example.net", "<", ">", "xxx",
                      "changeme", "replace-me")


def looks_like_placeholder(url: str) -> bool:
    """判断一个 base_url 是不是没填的占位符。

    为什么必须识别占位符，而不是只判断「空不空」
    --------------------------------------------
    照着部署文档做的人会把示例地址原样抄进 `.env`。填了「非空但无效」的值，
    比留空的破坏性大得多：

      - 留空 → 按请求头推导 → 管理页亮出「这是本机回环，发出去打不开」的警告
      - 填占位符 → 生成 `http://你的对外地址:8018/p/pmt-xxx`，
        **格式完全正常、警告不触发**，于是没人发现，直到业主打不开。

    所以这里把占位符当「没配」，让警告照常亮起来。
    宁可多提醒一次，也不要给出一个看起来很正式的错地址。

    ⚠ 判定是**子串匹配**，所以标记词必须挑「不可能出现在真域名里」的。
    两个反面例子：
      - 放过 "todo" → `https://todos.pmdemo.cn` 被误判；
      - 放过 "example" → `https://pm.example.cn` 被误判。
    所以改成精确的 `example.com/org/net`（IANA 保留域名，真公司不会用）。

    误判和漏判的后果**一样糟**：两者都会让 `_base_url` 走回退分支，
    生成一个业主打不开的内网地址，而管理页的 localhost 警告只在
    `127.0.0.1`/`localhost` 时才亮。所以两边都得尽量收紧。
    """
    u = (url or "").strip()
    if not u:
        return True
    low = u.lower()
    return any(m in low for m in _PLACEHOLDER_MARKS)


def _portal_url_is_placeholder() -> bool:
    """`.env` 里的 PORTAL_BASE_URL 是不是占位符（抄了文档示例）。"""
    from ..config import PORTAL_BASE_URL
    return looks_like_placeholder(PORTAL_BASE_URL)


def _base_url(request: Request) -> str:
    """推导对外可访问的基地址。

    优先用配置 PORTAL_BASE_URL（业主从外网访问时，请求头推导出的是内网
    地址，发出去就是打不开的）；没有配置、或配置是占位符，才按请求头推导——
    `X-Forwarded-Proto/Host` 是为了将来套反向代理时不用改代码。

    走回退分支的另一个好处：管理页的 localhost_only 警告只在这条路上有效。
    占位符被当成有效地址时，警告不亮、链接却是错的——最坏的组合。
    """
    from ..config import PORTAL_BASE_URL
    if not looks_like_placeholder(PORTAL_BASE_URL):
        return PORTAL_BASE_URL.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"{proto}://{host}".rstrip("/")
