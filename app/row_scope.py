"""数据行级可见范围（行级权限）。

为什么需要它
------------
现有权限是「模块 × 动作」两维，粒度到**整页**。于是只有两个选择：

- 全开放 —— 设计师能看全公司项目的合同额、成本、毛利，老板不放心；
- 不给看 —— 设计师看不到任何项目，等于没法干活。

现实中要的是第三种：**按人/按项目**看。所以这里补第三维。

## 三种范围
----------

| 范围 | 含义 | 谁用 |
|---|---|---|
| `all` 全部数据 | 不限制 | 老板 / admin、财务部、默认 |
| `assigned` 我负责的 | 项目负责人是我（designer/foreman/supervisor 含自己） | 设计师、工长 |
| `own` 我创建的 | 自己创建的项目 | 项目经理 |
| `none` 无数据 | 看不到任何项目 | 外包/临时账号 |

## 为什么存字符串而不是外键到 users.id
------------------------------------
`Project.designer` / `foreman` / `supervisor` 是 `String(50)` 自由文本
（历史原因，现场写「张工」）。它们**不是** users 外键，所以：

- 行级权限只能做**文本包含匹配**，做不了精确的 id 匹配；
- 这带来两个后果，必须写进注释而不是留给后人踩：
  1. 「李工」会同时匹配到「李工」和「李工强」——所以**匹配用 `in`**，
     并在 UI 上提示「按姓名匹配，不要只写姓」。
  2. 改名后历史数据对不上。所以配套动作是「认领/绑定账号」（见 tasks 模块
     里的做法）：文本用于展示，外键用于精确筛选，两者并存。

**精确筛选的正解是 `projects.owner_user_id`**，见 models.py。
本模块的文本匹配是给「现场随手写的名字」兜底的，不是最终方案。
"""
from __future__ import annotations

from sqlalchemy import or_

# ---------------------------------------------------------------- 范围定义

SCOPE_ALL = "all"
SCOPE_ASSIGNED = "assigned"   # 项目负责人含我
SCOPE_OWN = "own"             # 我创建的
SCOPE_NONE = "none"           # 看不到任何

SCOPES = [SCOPE_ALL, SCOPE_ASSIGNED, SCOPE_OWN, SCOPE_NONE]

SCOPE_LABELS = {
    SCOPE_ALL: "全部项目",
    SCOPE_ASSIGNED: "我负责的项目",
    SCOPE_OWN: "我创建的项目",
    SCOPE_NONE: "无项目权限",
}

SCOPE_HELP = {
    SCOPE_ALL: "不受限制，与现在的行为一致。",
    SCOPE_ASSIGNED: "项目负责人（设计/工长/监理）里含你的项目。"
                    "按姓名文本匹配，请写全名。",
    SCOPE_OWN: "你自己创建的项目。",
    SCOPE_NONE: "看不到任何项目（可用于外包、临时账号）。",
}


def normalize(scope: str) -> str:
    """把任意输入收敛到合法范围。认不出来就当 `none`（fail-closed）。

    刻意不回落 `all`：范围配错时的正确表现是「看不到东西」，
    老板会发现；回落 `all` 则是「看到了不该看的」，没人会发现。
    """
    s = (scope or "").strip().lower()
    return s if s in SCOPES else SCOPE_NONE


# ---------------------------------------------------------------- 用户范围读取

def user_scope(user) -> str:
    """某用户的数据范围。

    读不到（老账号没有 scope 行）时默认 `all` —— 这是**有意**的：
    升级后所有现有账号行为不变，不会有人的项目突然消失。
    收紧范围必须由人在 OA 页面上主动设置，这是「加权限」而不是「删权限」。
    """
    if not user:
        return SCOPE_NONE
    if getattr(user, "role", "") in ("老板", "admin"):
        return SCOPE_ALL
    return normalize(getattr(user, "data_scope", None) or SCOPE_ALL)


def scope_of(db, user) -> str:
    """带数据库读取的版本（用户对象可能是旧结构，缺字段时兜底）。"""
    return user_scope(user)


# ---------------------------------------------------------------- 查询过滤

def project_scope_filter(user, Project, *, require_own_field: bool = True):
    """返回「项目可见范围」的 SQL 条件。

    ⚠ 认不出的 scope 返回**恒假**条件（`1==0`）而不是放行：
    范围配置写错时宁可让人看不到项目，也不能让人看到全公司。

    `require_own_field=False` 时不生成 `owner_user_id` 条件（该列不存在时用）。
    """
    scope = user_scope(user)
    uid = getattr(user, "id", None)
    uname = (getattr(user, "display_name", "") or "").strip()
    urole = getattr(user, "role", "") or ""

    if urole in ("老板", "admin"):
        return None   # None = 不限制
    if scope == SCOPE_ALL:
        return None
    if scope == SCOPE_NONE or uid is None:
        return False  # 恒假

    conds = []
    # 负责人文本匹配。用 in 而不是 ==，兼容 "张工（现场）" 这类写法。
    for col in (Project.designer, Project.foreman, Project.supervisor):
        if uname:
            conds.append(col.like(f"%{uname}%"))
    if require_own_field and hasattr(Project, "owner_user_id"):
        conds.append(Project.owner_user_id == uid)

    if scope == SCOPE_OWN:
        # own 只认创建者，不含「被指为负责人」
        if require_own_field and hasattr(Project, "owner_user_id"):
            return Project.owner_user_id == uid
        return False

    # assigned：负责人命中
    if not conds:
        return False
    return or_(*conds)


def visible_projects(db, user, Project):
    """查当前用户可见的项目列表（最常用的入口）。"""
    cond = project_scope_filter(user, Project)
    q = db.query(Project)
    if cond is not None:
        q = q.filter(cond)
    return q.all()


def can_see_project(user, project, Project) -> bool:
    """判断单个项目是否可见。用于详情页 / 编辑页的二次校验。"""
    scope = user_scope(user)
    if getattr(user, "role", "") in ("老板", "admin") or scope == SCOPE_ALL:
        return True
    if scope == SCOPE_NONE:
        return False
    if project is None:
        return False
    if scope == SCOPE_OWN:
        return getattr(project, "owner_user_id", None) == getattr(user, "id", None)
    uname = (getattr(user, "display_name", "") or "").strip()
    uid = getattr(user, "id", None)
    names = [str(project.designer or ""), str(project.foreman or ""),
             str(project.supervisor or "")]
    if uname and any(uname in n for n in names if n):
        return True
    if hasattr(project, "owner_user_id") and uid is not None:
        return project.owner_user_id == uid
    return False
