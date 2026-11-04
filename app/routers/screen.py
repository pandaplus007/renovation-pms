"""经营大屏路由：/dashboard/screen。

⚠ 为什么挂在 /dashboard 下而不是新开一个模块：

    `permissions.resolve_module()` 对 `/dashboard` 开头的前缀直接返回
    "dashboard" 模块，所以本路由**自动继承**首页的模块权限，
    不需要改 MODULES、不需要改 LEGACY_ROLE_MODULES、不需要重播种权限矩阵。

    GET 路由的动作在 `build_route_index` 里默认兜底为 "view"，
    所以也**不需要**登记 app/_route_actions.json（那条只针对写操作路由）。

    结果：`test_all_routes.py` 会自动把这个页面纳入 GET 巡检。

⚠ 行级权限：见下面 project_id 的处理，以及 app/screen.py 的文件头。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..auth import login_required
from ..database import get_db
from ..models import Project, User
from ..row_scope import can_see_project, visible_projects
from ..screen import build
from ..web import render

router = APIRouter(tags=["screen"])


@router.get("/dashboard/screen")
def screen(request: Request, project_id: int = 0, db: Session = Depends(get_db),
           user: User = Depends(login_required)):
    """老板投屏用的经营大屏。

    带 `?project_id=N` 时切换成**单项目视图**（项目下拉里选）。

    刻意**不**继承 base.html：那个模板会把侧栏（24 个导航项）与顶栏
    渲染进来，投屏时既占地方又暴露系统结构。大屏自己有一个最小外壳，
    见 templates/screen.html。
    """
    # ---- 二次校验：URL 里的 ?project_id= 是可以手输的 ----
    # 下拉里只列可见项目挡不住它。返回 404 而不是 403：403 会泄露
    # 「这个项目存在」，404 不泄露任何信息（与 projects 详情页同一处置）。
    project = None
    if project_id:
        cand = db.get(Project, project_id)
        if cand is None or not can_see_project(user, cand, Project):
            raise HTTPException(status_code=404, detail="项目不存在")
        project = cand

    data = build(db, user, project=project)
    return render(request, "screen.html", {
        "active": "dashboard",
        # 侧栏「经营大屏」附加项的选中标记。与 `active` 分开是因为
        # 主项「项目总览」在 / 上，附加项在 /dashboard/screen 上，
        # 两者的 key 不同（见 app/permissions.py 的 MODULES.dashboard.extra_nav）。
        "active_sub": "screen",
        "user": user,
        **data,
    })