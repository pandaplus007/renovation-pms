"""模板与渲染公共设施。"""
import json
import os
import sys

from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, DATA_DIR, RESOURCE_DIR
from .models import (
    BUDGET_CATEGORIES, COST_CATEGORIES, CUSTOMER_STATUS, DOC_TYPES,
    INVENTORY_TYPES, INVOICE_DIRECTIONS, INVOICE_STATUS, INVOICE_TYPES,
    PAYMENT_STATUS, PAY_METHODS, PHASE_TEMPLATE, PO_STATUS, PROJECT_STATUS,
    RECORD_TYPES, TASK_STATUS,
)
from .permissions import ROLES as PERM_ROLES
from .permissions import ACTIONS, ACTION_LABELS, MODULES, NAV_GROUPS, nav_modules

TEMPLATES_DIR = os.path.join(RESOURCE_DIR, "app", "templates")
STATIC_DIR = os.path.join(RESOURCE_DIR, "app", "static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _css_version() -> str:
    """静态资源的缓存版本号：取全部 CSS 与增强脚本的 mtime 最大值。

    避免把版本号散落写在多个模板里（曾出现 base/login 两处不一致）。
    打包成单文件 exe 时静态资源被内嵌，退回固定版本号。

    ⚠ 清单里**每多一个文件都是一次「改完刷新、页面没变化」的假象来源**：
      以前只有 style.css 一个，看它就够。后来加了
      js/searchable-select.js 与 flt-search.js，
      「只改 JS 不改 CSS」于是变成一次假刷新——而这种假象极难自己发现，
      因为功能看起来「本来就没生效」，很容易被误判成「这段 JS 写错了」
      而查错方向。screen.css（经营大屏专用）也是同一类：只改它不改
      style.css 时，版本号必须跟着变。
      以后再加静态文件，记得加进这个列表。
    """
    if getattr(sys, "frozen", False):
        return "packed"
    newest = 0.0
    for rel in ("css/style.css", "css/screen.css", "css/basecamp.css", "css/linear.css",
                "js/searchable-select.js", "js/flt-search.js"):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(STATIC_DIR, *rel.split("/"))))
        except OSError:
            pass
    return str(int(newest)) if newest else "1"


templates.env.globals["CSS_VERSION"] = _css_version()


def money(v) -> str:
    """金额格式化：整数不显示小数，有小数保留 2 位（避免 4799.99 变 4800）。"""
    if v is None:
        return "0"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(fv - round(fv)) < 0.005:
        return f"{fv:,.0f}"
    return f"{fv:,.2f}"

templates.env.filters["money"] = money


def money_wan(v) -> str:
    """金额按「万」显示（大屏专用）。

    为什么大屏要单独一套：经营大屏是**投屏**用的，数字要在 3 米外读清。
    「20,597,845」是 10 个字符加千分位，在 1366 宽的投屏设备上
    5 个格子分下来每格只有 ~250px，放不下就得缩字号或截断——
    而截断一个金额比换个单位糟糕得多（老板会读到「20,597,」）。
    改成「2,059.8万」是 8 个字符，一眼能读完，且这是中文财报的通用记法。

    ⚠ 阈值 10000：小于 1 万的金额仍显示元（「8,500」而不是「0.85万」）。
       装修业务里小额（定金、尾款）不少，换算成万会得到「0.3万」这种
       比原值更难读的形式。

    ⚠ 这不是 `money` 的替代品，只是**大屏这一处**的显示单位。
       数据库、接口、导出、纸质单据一律仍用元——那里要的是精确值，
       不是投屏距离上的可读性。
    """
    if v is None:
        return "0"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(fv) < 10000:
        return money(v)
    # 保留一位小数：两位在小数点后是噪声（10,182,927.56 → 1,018.3万），
    # 但完全取整会让人以为「2,060万」是精确值（实际 2,059.78万）。
    return f"{fv / 10000:,.1f}万"

# 金额中文大写。纸质签证单必须有——小写金额可涂改，扯皮时无从对证。
from .utils.money_cn import amount_to_cn  # noqa: E402
templates.env.filters["money_cn"] = amount_to_cn

templates.env.filters["money_wan"] = money_wan

# 状态色统一入口：模板里写 {{ '施工中' | state_badge }}，
# 不再各页面各写一套 text-bg-{{ 'success' if ... }} 的映射。
# 收敛理由见 state_colors 模块文档。
from .state_colors import register as _register_state_colors  # noqa: E402
_register_state_colors(templates.env)

# 全局选项，模板可直接使用 OPTIONS.customer_status 等
OPTIONS = {
    "customer_status": CUSTOMER_STATUS,
    "project_status": PROJECT_STATUS,
    "budget_categories": BUDGET_CATEGORIES,
    "cost_categories": COST_CATEGORIES,
    "payment_status": PAYMENT_STATUS,
    "phase_template": PHASE_TEMPLATE,
    "po_status": PO_STATUS,
    "task_status": TASK_STATUS,
    "record_types": RECORD_TYPES,
    "doc_types": DOC_TYPES,
    "inventory_types": INVENTORY_TYPES,
    "roles": PERM_ROLES,
    "invoice_directions": INVOICE_DIRECTIONS,
    "invoice_types": INVOICE_TYPES,
    "invoice_status": INVOICE_STATUS,
    "pay_methods": PAY_METHODS,
}


# ---- 公司品牌信息（名称 / LOGO）----
COMPANY_FILE = os.path.join(DATA_DIR, "company.json")
_company_cache = None


def company_info() -> dict:
    """读取公司品牌信息（懒加载 + 文件缓存）。"""
    global _company_cache
    if _company_cache is None:
        try:
            with open(COMPANY_FILE, encoding="utf-8") as f:
                _company_cache = json.load(f)
        except Exception:
            _company_cache = {"name": "装修项目管家", "logo": ""}
    return _company_cache


def set_company(name: str = "", logo: str = ""):
    """保存公司品牌信息。"""
    global _company_cache
    _company_cache = {"name": name.strip() or "装修项目管家", "logo": logo.strip() or ""}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(COMPANY_FILE, "w", encoding="utf-8") as f:
        json.dump(_company_cache, f, ensure_ascii=False)


# ---- 导航栏文字自定义 ----
NAV_FILE = os.path.join(DATA_DIR, "nav_labels.json")
_nav_cache = None


def nav_labels() -> dict:
    """读取自定义导航文字（模块 key -> 文字），未设置时为空。"""
    global _nav_cache
    if _nav_cache is None:
        try:
            with open(NAV_FILE, encoding="utf-8") as f:
                _nav_cache = json.load(f)
        except Exception:
            _nav_cache = {}
    return _nav_cache


def set_nav_labels(labels: dict):
    """保存自定义导航文字。"""
    global _nav_cache
    _nav_cache = {k: v.strip() for k, v in labels.items() if v and v.strip()}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(NAV_FILE, "w", encoding="utf-8") as f:
        json.dump(_nav_cache, f, ensure_ascii=False)


def render(request, name: str, ctx: dict | None = None, status_code: int = 200):
    """渲染模板：自动注入全局选项、公司品牌信息与权限判断函数。"""
    ctx = ctx or {}
    user = ctx.get("user")
    # 侧边栏「按项目查看」树所需的项目。
    #
    # ⚠ 必须按当前用户的数据范围过滤（行级权限，见 app/row_scope.py）。
    # 这个树在**每一个页面**的侧栏里都渲染，且渲染的是**项目名 + 链接**——
    # 不过滤的话，收紧范围的用户在侧栏就能看到全部项目名，
    # 而点进去才 404，等于「看不见数据但看得见有哪些项目」。
    db = None
    try:
        from .database import SessionLocal
        from .models import Project
        from .row_scope import project_scope_filter
        db = SessionLocal()
        try:
            _q = db.query(Project)
            _cond = project_scope_filter(user, Project)
            if _cond is not None:
                _q = _q.filter(_cond)
            ctx.setdefault("projects", _q.order_by(Project.name).all())
        finally:
            db.close()
            db = None
    except Exception:
        pass

    opts = dict(OPTIONS)
    info = company_info()
    opts["company_name"] = info.get("name", "装修项目管家")
    opts["company_logo"] = info.get("logo", "")
    opts["nav_labels"] = nav_labels()
    opts["nav_modules"] = nav_modules()
    opts["nav_groups"] = NAV_GROUPS
    opts["nav_icons"] = {m.key: m.icon for m in MODULES.values()}
    ctx.setdefault("OPTIONS", opts)
    ctx.setdefault("PERM_MODULES", list(MODULES.values()))
    ctx.setdefault("PERM_ACTIONS", ACTIONS)
    ctx.setdefault("PERM_ACTION_LABELS", ACTION_LABELS)

    # 模板内用 can('projects', 'edit') 控制按钮显隐；需要真实数据库会话时即时打开
    def can(module: str, action: str) -> bool:
        from .database import SessionLocal as _SL
        from .permissions import can as _can
        if not user:
            return False
        s = _SL()
        try:
            return _can(user, module, action, s)
        finally:
            s.close()

    ctx.setdefault("can", can)
    _inject_approval(ctx)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _inject_approval(ctx: dict) -> None:
    """给单据页面注入审批状态，模板里直接 include `_approval_bar.html` 即可。

    约定：路由把单据主键放进 ctx["approval_id"]、类型放进 ctx["approval_entity"]
    （如 supplier_payment / purchase_contract / sub_contract / cost_item）。
    未启用的类型 approval_enabled=False，片段自动不渲染。
    """
    entity = ctx.get("approval_entity")
    eid = ctx.get("approval_id")
    if not entity or not eid:
        return
    try:
        from .approvals import approval_state, is_enabled
        from .database import SessionLocal
        s = SessionLocal()
        try:
            ctx.setdefault("approval_enabled", is_enabled(s, entity))
            ctx.setdefault("approval_state", approval_state(s, entity, eid))
        finally:
            s.close()
    except Exception:
        # 审批是附加能力，取不到状态时静默降级，绝不影响主业务页面渲染
        ctx.setdefault("approval_enabled", False)
        ctx.setdefault("approval_state", {"exists": False, "label": "未提交"})
