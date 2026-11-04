"""岗位权限系统：模块 × 动作 的权限矩阵、路由动作表与全局守卫。

设计要点
--------
1. **单一注册表** `MODULES`：模块 key / 名称 / 分组 / 路径前缀。侧边栏、OA 配置页、
   权限守卫全部从这里取，避免历史上三处硬编码列表互相不同步。
2. **动作位掩码**：`view / create / edit / delete / export / approve` 六个位。
3. **权限存库**：`role_module_perms`（岗位）+ `user_module_perms`（个人覆盖，优先）。
   首次启动从 `LEGACY_ROLE_MODULES` 播种，老数据行为保持不变。
4. **fail-closed**：`resolve_module` 认不出的路径一律拒绝（`/search`、`/api/*`
   等少数例外显式白名单），而不是像旧版那样默认放行。
5. **老板 / admin 是超级用户**：恒定拥有全部模块的全部动作，配置页也改不掉，
   防止把自己锁在门外。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request

from .auth import get_current_user
from .database import get_db
from .models import User

# ---------------------------------------------------------------- 动作

ACTIONS = ("view", "create", "edit", "delete", "export", "approve")

ACTION_BITS = {
    "view": 1, "create": 2, "edit": 4, "delete": 8, "export": 16, "approve": 32,
}

ACTION_LABELS = {
    "view": "查看", "create": "新增", "edit": "修改",
    "delete": "删除", "export": "导出", "approve": "审批",
}

ALL_ACTIONS_MASK = 63


def mask_to_actions(mask: int) -> set[str]:
    return {a for a in ACTIONS if mask & ACTION_BITS[a]}


def actions_to_mask(actions) -> int:
    m = 0
    for a in actions or ():
        m |= ACTION_BITS.get(a, 0)
    return m


# ---------------------------------------------------------------- 模块注册表

@dataclass(frozen=True)
class ModuleDef:
    key: str
    label: str          # 默认导航文字（可被 data/nav_labels.json 覆盖）
    group: str          # 侧边栏分组
    prefixes: tuple = ()  # 路由前缀；长前缀优先匹配
    icon: str = "bi-circle"  # 侧边栏图标
    in_nav: bool = True   # 是否出现在侧边栏
    pending: bool = False  # 仅登记、尚未实现路由（自检时跳过，避免误报）
    note: str = ""
    nav_url: str = ""    # 侧栏点击跳转地址；空则退回 prefixes[0]
    # 附加导航项 ((key, label, url, icon), ...)：渲染在主项**下面**的次级链接。
    #
    # 为什么需要它：一个模块只能有一个 nav_url，而有些模块下确实有多个
    # 值得从侧栏直达的页面（dashboard 下的 /dashboard/screen 经营大屏）。
    # 为此给每个模块都加第二个 key 是不行的——那会让 20 个模块的权限判定
    # 都变成「一个 key 对两个 URL」，而 nav_url/prefixes 的分裂正是为了
    # 避免这类耦合。附加项只影响**渲染**，不参与权限：
    # 它的可见性完全跟随宿主模块（模板里在同一个 can(key,'view') 分支内）。
    extra_nav: tuple = ()

    # prefixes 与 nav_url 为什么必须分开
    # -----------------------------------
    # prefixes 是**权限匹配前缀**：判断某个 URL 归哪个模块管。比如
    # reports 的 prefixes=('/reports',) 要同时覆盖 /reports/ledger 和
    # /reports/profit，所以它必须保持粗粒度。
    #
    # 但侧栏拿 prefixes[0] 当跳转地址就会出事：/reports 本身没有任何
    # 路由（页面都在 /reports/xxx 下），于是侧栏「项目台账」点进去是
    # 404 {"detail":"Not Found"}——本地和 NAS 上都是。这个死链存在很久，
    # 之前没被发现是因为没有自动化检查去点导航。
    #
    # 修法不是把 prefixes 改成 ('/reports/ledger',)：那会让 /reports/profit
    # 匹配不上任何模块，权限直接失效。必须给导航单独一个地址字段。


MODULES: dict[str, ModuleDef] = {m.key: m for m in [
    ModuleDef("dashboard",        "项目总览", "经营", ("/dashboard",), "bi-speedometer2", note="首页与全局预警",
               extra_nav=(("screen", "经营大屏", "/dashboard/screen", "bi-display"),)),
    ModuleDef("todo",             "待办总览", "经营", ("/todo",), "bi-list-check", note="应收逾期、超付、送货与分包缺口等异常汇总"),
    ModuleDef("customers",        "客户管理", "经营", ("/customers",), "bi-people"),
    ModuleDef("projects",         "项目管理", "项目", ("/projects",), "bi-briefcase",
               note="项目档案、收款、预算成本、任务、现场记录"),
    ModuleDef("gantt",            "工期规划", "项目", ("/gantt",), "bi-bar-chart"),
    # /tasks 必须排在 /projects 之后无关（首段不同），但它要独立成模块：
    # 挂在 projects 下的话，「我的任务」会随项目模块一起被关掉，
    # 而它恰恰是唯一与岗位无关、人人都该能用的视图。
    ModuleDef("tasks",            "任务", "项目", ("/tasks",), "bi-list-check",
               note="跨项目任务总览与「我的任务」（按人/按时间/按状态）"),
    ModuleDef("expenses",         "费用管理", "项目", ("/expenses",), "bi-wallet2"),
    ModuleDef("reports",          "项目台账", "项目", ("/reports",), "bi-journal-text", note="台账与毛利报表",
               nav_url="/reports/ledger"),
    ModuleDef("changes",          "变更签证", "项目", ("/changes",), "bi-clipboard-check",
               note="施工增减项：签字确认后计入结算金额与毛利"),
    ModuleDef("quotes",           "报价单", "经营", ("/quotes",), "bi-file-earmark-spreadsheet",
               note="报价到成交的签单漏斗、流失原因、单价基准"),
    ModuleDef("sales_invoices",   "开票申请", "财务", ("/sales-invoices",), "bi-receipt-cutoff",
               note="向客户开票的申请台账：已收 vs 已开 vs 待开"),
    ModuleDef("procurement",      "采购", "采招", ("/procurement", "/contracts", "/purchases"), "bi-cart3",
               note="采购合同 + 采购单（合并页）"),
    ModuleDef("subcontracts",     "分包管理", "采招", ("/subcontracts",), "bi-diagram-3"),
    ModuleDef("suppliers",        "供应商", "采招", ("/suppliers",), "bi-shop"),
    ModuleDef("materials",        "材料管理", "采招", ("/materials",), "bi-box-seam"),
    ModuleDef("inventory",        "库存管理", "采招", ("/inventory",), "bi-archive"),
    ModuleDef("invoices",         "发票管理", "财务", ("/invoices",), "bi-receipt"),
    # 注意：/sales-invoices 必须排在 /invoices 之前登记前缀匹配？——不需要，
    # 两者首段不同（/sales- vs /inv），不构成前缀包含关系。
    ModuleDef("supplier_payments", "供应商付款", "财务", ("/supplier-payments", "/reconcile"), "bi-cash-coin",
               note="付款登记、供应商对账、银行流水对账"),
    ModuleDef("payables",          "应付账款", "财务", ("/payables",), "bi-wallet2",
               note="按合同看欠款：近期应付 / 潜在应付 / 总敞口"),
    ModuleDef("approvals",        "审批中心", "采招", ("/approvals",), "bi-check2-square",
               note="付款与合同的串行审批（只留痕，不改记账）"),
    ModuleDef("company_docs",     "公司资料", "系统", ("/company-docs",), "bi-journal-richtext"),
    # 人员名册：管「人」，不管「账号」。现场工长/师傅不需要开账号也能进名册，
    # 从而能被指派任务。与 oa_users 分开是因为权限粒度不同——
    # 能维护名册的人（项目经理、行政）通常不该有开账号、改权限的权力。
    ModuleDef("staff",            "公司人员", "系统", ("/staff",), "bi-people",
               note="人员名册：工长/师傅/设计师，不需要登录账号也能被指派任务"),
    ModuleDef("phases",           "阶段模板", "系统", ("/phases",), "bi-layers", in_nav=False),
    ModuleDef("oa_users",         "OA管理", "系统", ("/oa/users",), "bi-shield-lock", note="账号与岗位（高敏感）"),
    ModuleDef("oa_perms",         "岗位权限", "系统", ("/oa/permissions", "/oa/roles"), "bi-diagram-2",
               note="岗位权限矩阵与自定义岗位（高敏感）"),
    ModuleDef("oa_audit",         "审计日志", "系统", ("/oa/audit",), "bi-journal-check"),
    # /oa/notify-test 必须落在 oa_settings 这个前缀里，否则 resolve_module
    # 认不出它 → fail-closed 直接 403。症状是「公司设置页上的测试推送按钮
    # 点了没反应」，而 GET 全路由测试会把它当死链报出来。
    ModuleDef("oa_settings",      "公司设置", "系统", ("/oa/settings", "/oa/notify-test"), "bi-building"),
]}

ALL_MODULES = set(MODULES)

NAV_GROUPS = ("经营", "项目", "采招", "财务", "系统")


def nav_modules() -> list[ModuleDef]:
    """按分组顺序返回需要在侧边栏显示的模块。"""
    out = []
    for g in NAV_GROUPS:
        out.extend(m for m in MODULES.values() if m.in_nav and m.group == g)
    return out


# ---------------------------------------------------------------- 岗位

BUILTIN_ROLES = ["老板", "工程部", "财务部", "采购部", "行政部", "员工"]

# 兼容历史数据里的 admin 岗位
SUPER_ROLES = {"老板", "admin"}


def is_superuser(user) -> bool:
    return bool(user) and getattr(user, "role", "") in SUPER_ROLES


# 旧版模块可见性矩阵（只记模块、不分动作），仅用于首次播种数据库。
# 播种时给全量动作位，保证迁移前后行为完全一致；上线后由 OA 权限矩阵页收紧。
LEGACY_ROLE_MODULES: dict[str, set[str]] = {
    "老板": set(ALL_MODULES),
    "admin": set(ALL_MODULES),
    "工程部": {"dashboard", "customers", "projects", "gantt", "materials",
               "inventory", "reports", "procurement", "subcontracts", "expenses", "company_docs"},
    "财务部": {"dashboard", "customers", "projects", "reports",
               "invoices", "supplier_payments", "expenses", "company_docs"},
    "采购部": {"dashboard", "projects", "reports", "procurement", "subcontracts",
               "materials", "suppliers", "inventory", "company_docs"},
    "行政部": {"dashboard", "customers", "suppliers", "projects", "reports",
               "expenses", "phases", "company_docs"},
    "员工": {"dashboard", "projects"},
}

# 审批中心（approvals）的岗位默认权限。
# 为什么单独列：审批是后加的模块，不在 LEGACY_ROLE_MODULES 里。若不单独播种，
# 新模块对所有现有岗位都是「无权限」，结果就是只有超级用户能用——财务部、采购部
# 这些本该走审批的岗位反而被挡在门外（fail-closed 的正确结果，但不是可用状态）。
#
# flags 位：view=1 create=2 edit=4 delete=8 export=16 approve=32
#   - 老板：全量（流程配置在这里改）
#   - 财务部 / 采购部 / 工程部：可查看、可提交、可审批（approve）
#     但不能改流程配置（无 edit）——流程是老板定的规矩
#   - 行政部：只查看
#   - 员工：无（由岗位级 role_module_perms 缺失即 0 实现，不在此登记）
APPROVAL_MODULE = "approvals"
_A, _V, _C, _E, _D, _X = 32, 1, 2, 4, 8, 16   # approve/view/create/edit/delete/export
APPROVAL_DEFAULT_PERMS: dict[str, int] = {
    "老板": _V | _C | _E | _A,           # 含 edit：可配置流程
    "admin": _V | _C | _E | _A,
    "财务部": _V | _C | _A,
    "采购部": _V | _C | _A,
    "工程部": _V | _C | _A,
    "行政部": _V,
}

# 后加模块（审批中心、应付账款）的岗位默认权限。
# 与 APPROVAL_DEFAULT_PERMS 同理：不单独播种的话，新模块对所有现有岗位都是
# 「无权限」，只有超级用户能用——财务部看不到应付账款，正是最不该发生的事。
# 动作位：view=1 create=2 edit=4 delete=8 export=16 approve=32
PAYABLES_MODULE = "payables"
PAYABLES_DEFAULT_PERMS: dict[str, int] = {
    "老板": _V | _X,          # 可看可导出；台账只读，不给 create/edit
    "admin": _V | _X,
    "财务部": _V | _X,        # 财务看欠款是天职
    "采购部": _V | _X,        # 采购要知道自己欠供应商多少
    "工程部": _V,             # 只看，不导出
    "行政部": _V,
}

# 变更签证 / 报价单 / 开票申请 三个后加模块的岗位默认权限。
#
# 分配依据是「谁在业务上产生这个单据」：
#   - 变更签证由工程部在现场产生、老板确认金额，所以工程部可写、老板全量
#   - 报价单由老板/行政对外报，所以只有老板与行政部能建
#   - 开票申请是财务的动作，其他部门只能看（避免谁都能填个开票单出去）
#
# flags 位：view=1 create=2 edit=4 delete=8 export=16 approve=32
#   老板 / admin 含 delete——录错了要能删；其余岗位不给 delete，
#   走「标记作废/已驳回」而不是物理删除（见 sales_invoices 的状态设计）。
CHANGES_MODULE = "changes"
QUOTES_MODULE = "quotes"
SALES_INVOICES_MODULE = "sales_invoices"

DEFAULT_PERMS_EXTRA: dict[str, dict[str, int]] = {
    CHANGES_MODULE: {
        "老板": _V | _C | _E | _D | _X | _A,
        "admin": _V | _C | _E | _D | _X | _A,
        "工程部": _V | _C | _E | _X,        # 现场提变更，可确认状态
        "财务部": _V | _X,                  # 变更影响结算金额，要能看
        "采购部": _V,                       # 只看，判断改需求会不会影响已下单的材料
        "行政部": _V,
    },
    QUOTES_MODULE: {
        "老板": _V | _C | _E | _D | _X,
        "admin": _V | _C | _E | _D | _X,
        "行政部": _V | _C | _E | _X,        # 行政常做报价初稿
        "工程部": _V | _C,                  # 工程部量房后报量
        "财务部": _V,                       # 只看漏斗与单价基准
        "采购部": _V,
    },
    SALES_INVOICES_MODULE: {
        "老板": _V | _C | _E | _D | _X | _A,
        "admin": _V | _C | _E | _D | _X | _A,
        "财务部": _V | _C | _E | _D | _X,   # 开票是财务的天职，全权
        "行政部": _V | _C,                  # 常代客户跑票
        "工程部": _V,
        "采购部": _V,
    },
}

# 兼容旧名字符串（老模板 / 老数据仍可能引用）
ROLES = BUILTIN_ROLES


def legacy_module_set(role: str) -> set[str]:
    return LEGACY_ROLE_MODULES.get(role, set())


# ---------------------------------------------------------------- 路由动作表
# 键为路由模板（FastAPI 的 path_format），值为动作名。
# 由全量路由清单生成，任何一条 POST/PUT/DELETE 路由都必须在此登记；
# 未登记的写操作一律拒绝（见 _route_index 的兜底），不会出现"漏登记=放行"。

def _load_route_actions() -> dict[str, str]:
    from pathlib import Path
    import os
    data_path = Path(__file__).with_name("_route_actions.json")
    if not data_path.exists():
        return {}
    import json
    try:
        return json.loads(data_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


ROUTE_ACTIONS: dict[str, str] = _load_route_actions()

# 免鉴权前缀：静态资源、登录、业主只读链接，以及需要自行按权限过滤结果的接口。
# 注意 /search 与 /api/alerts 在这里放行，但内部会按用户的模块权限过滤输出，
# 否则任何登录用户都能搜到/收到全公司的发票号与供应商名。
#
# `/p/` 是**业主**入口：凭 token 进、不登录，内部只暴露 build_view()
# 里挑过的那几项字段（进度/照片/收款，不含任何单价与供应商名）。
# 漏登记的后果是业主打开链接得到 403，而日志里只有一条无权限。
PUBLIC_PREFIXES = ("/login", "/logout", "/static/", "/uploads/", "/api/", "/search", "/p/")


# ---------------------------------------------------------------- 模块解析

def resolve_module(path: str) -> str | None:
    """根据请求路径解析所属模块。认不出来返回 None（由调用方决定放行/拒绝）。"""
    if path == "/" or path.startswith("/dashboard"):
        return "dashboard"
    best_key, best_len = None, -1
    for m in MODULES.values():
        for pre in m.prefixes:
            if path.startswith(pre) and len(pre) > best_len:
                best_key, best_len = m.key, len(pre)
    return best_key


# ---------------------------------------------------------------- 路由索引

def _path_to_regex(path_format: str) -> re.Pattern:
    """把 '/projects/{project_id}/edit' 转成匹配用的正则。"""
    parts = []
    for seg in re.split(r"(\{[^}]+\})", path_format):
        if seg.startswith("{") and seg.endswith("}"):
            parts.append("[^/]+")
        else:
            parts.append(re.escape(seg))
    return re.compile("^" + "".join(parts) + "$")


@dataclass
class _RouteIndex:
    """(正则, 模块, 动作, 方法) 列表；静态路由排在动态路由前面，保证 /x/new 不被 /x/{id} 吃掉。

    匹配必须带 HTTP 方法：同一路径常同时有 GET 与 POST（如 `/approvals/flows`
    的查看用 view、保存用 edit）。只按路径匹配时，先命中的那条会覆盖另一条的方法
    ——表现为「只读权限的人也能提交表单」，因为它的 POST 被 GET 的 view 规则接住了。
    """
    entries: list = field(default_factory=list)

    def lookup(self, path: str, method: str = "GET"):
        m = (method or "GET").upper()
        for rx, mod, act, em in self.entries:
            if rx.match(path) and (m in em or "GET" in em and m == "GET"):
                return mod, act
        return None, None


def _iter_effective_routes(routes) -> list:
    """把 app.routes 展开成真正参与匹配的路由列表。

    FastAPI 0.141 起 `include_router` 改成惰性：app.routes 里放的是 `_IncludedRouter`
    占位符，业务路由要靠 `iter_route_contexts()` 递归展开才拿得到。直接遍历 app.routes
    只能看到 /login、/logout 寥寥数条，索引一旦残缺，守卫的 fail-closed 就会把
    **所有**已登录用户的业务请求一律判成 403（含老板）。旧版 FastAPI 走 fallback 分支。
    """
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:
        return list(routes)
    return [ctx.route for ctx in iter_route_contexts(routes)]


def build_route_index(routes) -> _RouteIndex:
    idx = _RouteIndex()
    prepared = []
    for r in _iter_effective_routes(routes):
        path_format = getattr(r, "path_format", None) or getattr(r, "path", None)
        methods = getattr(r, "methods", None) or set()
        if not path_format or not path_format.startswith("/"):
            continue
        if not (methods & {"GET", "POST", "PUT", "DELETE"}):
            continue
        if path_format.startswith(("/static", "/uploads")):
            continue
        mod = resolve_module(path_format)
        if mod is None:
            continue
        # 一条路由可能声明多个方法（如 GET+POST），每个方法单独建一条索引记录
        for m in ("GET", "POST", "PUT", "DELETE"):
            if m not in methods:
                continue
            prepared.append((path_format.count("{"), path_format, mod, m))

    prepared.sort(key=lambda x: (x[0], -len(x[1])))
    for _, path_format, mod, method in prepared:
        act = ROUTE_ACTIONS.get(f"{method} {path_format}")
        if act is None:
            # GET 兜底为 view；写操作无登记 → 标记为 None，请求时拒绝
            act = "view" if method == "GET" else None
            if method != "GET":
                import sys
                print(f"[permissions] 警告：路由未登记动作，已默认拒绝 -> {method} {path_format}",
                      file=sys.stderr)
        # 同一路径+同一方法只保留一条（后写的覆盖先写的）
        if any(e[0].pattern == _path_to_regex(path_format).pattern and method in e[3]
               for e in idx.entries):
            continue
        idx.entries.append((_path_to_regex(path_format), mod, act, {method}))
    return idx


_ROUTE_INDEX: _RouteIndex | None = None


def init_route_index(app) -> None:
    global _ROUTE_INDEX
    idx = build_route_index(app.routes)
    _ROUTE_INDEX = idx
    _warn_if_index_incomplete(idx)


def _warn_if_index_incomplete(idx: "_RouteIndex") -> None:
    """索引残缺是静默灾难：守卫 fail-closed 会把所有人（含老板）的业务页判成 403，
    而日志一行错都没有。这里按「每个注册模块都要有可匹配路由」自检，
    覆盖两类事故：新增模块忘了挂路由、以及框架改了路由展开方式。
    """
    import sys
    covered = {e[1] for e in idx.entries if e[1]}
    missing = sorted(k for k in ALL_MODULES - covered if not MODULES[k].pending)
    if not missing:
        return
    print("=" * 66, file=sys.stderr)
    print("[permissions][严重] 路由索引不完整，以下模块没有任何可匹配路由：", file=sys.stderr)
    for key in missing:
        print(f"    - {key}（{MODULES[key].label}）", file=sys.stderr)
    print("  后果：已登录用户访问上述模块会一律收到 403，且日志中查不到原因。", file=sys.stderr)
    print("  排查：确认 build_route_index 是否拿到了展开后的完整路由列表"
          "（FastAPI 版本升级可能改变 include_router 的展开时机）。", file=sys.stderr)
    print("=" * 66, file=sys.stderr)


def lookup_route(path: str, method: str = "GET"):
    if _ROUTE_INDEX is None:
        return None, None
    return _ROUTE_INDEX.lookup(path, method)


# ---------------------------------------------------------------- 权限读取

_PERM_CACHE: dict | None = None


def invalidate_perm_cache() -> None:
    global _PERM_CACHE
    _PERM_CACHE = None


def _load_perms(db) -> dict:
    """读出 role→{module:flags} 与 user→{module:flags}，带进程内缓存。"""
    global _PERM_CACHE
    if _PERM_CACHE is not None:
        return _PERM_CACHE
    from .models import RoleModulePerm, UserModulePerm
    role_map: dict[str, dict[str, int]] = {}
    user_map: dict[int, dict[str, int]] = {}
    for row in db.query(RoleModulePerm).all():
        role_map.setdefault(row.role, {})[row.module] = int(row.flags or 0)
    for row in db.query(UserModulePerm).all():
        user_map.setdefault(row.user_id, {})[row.module] = int(row.flags or 0)
    _PERM_CACHE = {"role": role_map, "user": user_map}
    return _PERM_CACHE


def flags_for(user, module: str, db) -> int:
    """某用户在某模块上的有效动作位掩码。个人覆盖优先于岗位。"""
    if is_superuser(user):
        return ALL_ACTIONS_MASK
    if module not in ALL_MODULES:
        return 0
    data = _load_perms(db)
    um = data["user"].get(getattr(user, "id", None))
    if um is not None and module in um:
        return um[module]
    return data["role"].get(getattr(user, "role", ""), {}).get(module, 0)


def can(user, module: str, action: str, db) -> bool:
    if not user or not getattr(user, "is_active", True):
        return False
    return bool(flags_for(user, module, db) & ACTION_BITS.get(action, 0))


def allowed_modules(user, db) -> set[str]:
    """用户至少可查看的模块集合（侧边栏 / 搜索过滤用）。"""
    if is_superuser(user):
        return set(ALL_MODULES)
    data = _load_perms(db)
    um = data["user"].get(getattr(user, "id", None), {})
    rm = data["role"].get(getattr(user, "role", ""), {})
    out = set()
    for m in ALL_MODULES:
        f = um.get(m, rm.get(m, 0))
        if f & ACTION_BITS["view"]:
            out.add(m)
    return out


def role_matrix(db) -> dict:
    """岗位 → {module: flags}，供 OA 配置页渲染。"""
    data = _load_perms(db)
    out: dict[str, dict[str, int]] = {}
    for role in BUILTIN_ROLES:
        out[role] = dict(data["role"].get(role, {}))
    for role, mods in data["role"].items():
        out.setdefault(role, dict(mods))
    return out


# ---------------------------------------------------------------- 守卫

def role_guard(request: Request,
               user: User | None = Depends(get_current_user),
               db=Depends(get_db)):
    """全局权限守卫。

    - 未登录放行（由各路由的 login_required 负责跳转登录页）
    - 公共前缀放行
    - 其余一律 fail-closed：模块认不出、或动作未登记、或无权限 → 403
    """
    path = request.url.path
    method = request.method or "GET"
    if path.startswith(PUBLIC_PREFIXES):
        return
    if user is None:
        return
    request.state.user = user

    mod, act = lookup_route(path, method)
    if mod is None:
        raise HTTPException(status_code=403, detail="无权限访问该地址")
    if act is None:
        raise HTTPException(status_code=403, detail="该操作未配置权限，已拒绝")
    if not can(user, mod, act, db):
        raise HTTPException(status_code=403,
                            detail=f"无「{ACTION_LABELS.get(act, act)}」权限：{MODULES[mod].label}")
