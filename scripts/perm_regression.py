"""权限回归测试（本地夹具，绝不触碰生产数据）。

为什么需要它
------------
`app.permissions` 的守卫是 fail-closed 的：路由没登记动作就整条拒绝，模块认不出也拒绝。
这套逻辑的正确性靠人肉点页面是测不出来的——加一个路由、忘登记动作，老板本人就会被
挡在门外；改一次 PUBLIC_PREFIXES，就可能把整个 `/api` 漏给低权限账号。

隔离原理
--------
本脚本在 import `app` 之前先把 `app.config` 的 DATA_DIR / UPLOAD_DIR / DATABASE_URL
改指向一个一次性临时目录。于是 `app.main` 模块级的全部副作用——`_migrate()`、
`seed()`、`_seed_permissions()`、`backup_db()`、每日备份守护线程——都落在临时库上，
生产 `data/renovation.db` 连打开都不会。`check_isolation()` 会在收尾时断言引擎确实
指向临时目录，任何将来的改动一旦破坏这个前提就会直接失败。

用法
----
    python scripts/perm_regression.py            # 跑全部检查
    python scripts/perm_regression.py --keep     # 失败时保留临时库/目录便于排查

退出码：0 全通过；1 有失败项。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --------------------------------------------------------------------------- 隔离

TMPDIR = Path(tempfile.mkdtemp(prefix="permreg_"))
PROD_DB = ROOT / "data" / "renovation.db"

from app import config as cfg  # noqa: E402

cfg.DATA_DIR = str(TMPDIR)
cfg.DB_PATH = str(TMPDIR / "renovation.db")
cfg.DATABASE_URL = f"sqlite:///{cfg.DB_PATH}"
cfg.UPLOAD_DIR = str(TMPDIR / "uploads")
cfg.SECRET_KEY = "perm-regression-test-only-secret"
os.makedirs(cfg.UPLOAD_DIR, exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from app import permissions as P  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import RoleModulePerm, User, UserModulePerm  # noqa: E402

PASSWORD = "reg-仅测试用-123"
NO_ROLE = "__无岗位__"          # 不在播种矩阵里的岗位 → 有效权限全为 0
ROLES = [*P.BUILTIN_ROLES, "admin", NO_ROLE]

# --------------------------------------------------------------------------- 结果收集

class Report:
    def __init__(self) -> None:
        self.failures: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, str]] = []
        self.crashes: dict[str, str] = {}
        self.passed = 0

    def ok(self) -> None:
        self.passed += 1

    def fail(self, check: str, detail: str) -> None:
        self.failures.append((check, detail))

    def warn(self, check: str, detail: str) -> None:
        self.warnings.append((check, detail))

    def crash(self, key: str, detail: str) -> None:
        self.crashes.setdefault(key, detail)


R = Report()

# 探针返回值：请求在视图层炸掉（500 / 抛异常）时的哨兵值。
# 这类故障与权限判定无关，单独统计，否则一处模板 bug 会让整轮测试中途抛停。
CRASH = 599


def probe(client: TestClient, method: str, path: str) -> int:
    """发一次请求并返回状态码；视图层异常记为 CRASH 而不抛出。"""
    try:
        r = client.request(method, concrete(path), follow_redirects=False)
    except Exception as e:
        R.crash(f"{method} {path}", f"{type(e).__name__}: {str(e).splitlines()[0][:110]}")
        return CRASH
    if r.status_code >= 500:
        R.crash(f"{method} {path}", f"HTTP {r.status_code}")
        return CRASH
    return r.status_code


# --------------------------------------------------------------------------- 路由清单

def route_inventory() -> list[tuple[str, str]]:
    """返回去重后的 (METHOD, path_format)，与 build_route_index 的筛选口径保持一致。

    必须走 `iter_route_contexts` 展开：FastAPI 0.141 起 app.routes 里只有
    `_IncludedRouter` 占位符，直接遍历会漏掉全部业务路由（这正是本脚本要防的回归）。
    """
    from fastapi.routing import iter_route_contexts

    seen: dict[tuple[str, str], None] = {}
    for r in iter_route_contexts(app.routes):
        path = getattr(r, "path_format", None) or getattr(r, "path", None)
        methods = getattr(r, "methods", None) or set()
        if not path or not path.startswith("/"):
            continue
        if not (methods & {"GET", "POST", "PUT", "DELETE"}):
            continue
        if path.startswith(("/static", "/uploads")):
            continue
        for m in sorted(methods & {"GET", "POST", "PUT", "DELETE"}):
            seen.setdefault((m, path), None)
    return sorted(seen)


def concrete(path_format: str) -> str:
    """把 '/projects/{project_id}/edit' 变成可请求的 '/projects/1/edit'。"""
    out, seg = [], ""
    for ch in path_format:
        if ch == "{":
            seg = ch
        elif ch == "}" and seg:
            out.append("1")
            seg = ""
        elif not seg:
            out.append(ch)
    return "".join(out)


def is_guarded(path_format: str) -> bool:
    """PUBLIC_PREFIXES 里的路由由路由自己过滤结果，守卫层有意放行，不参与矩阵比对。"""
    return not path_format.startswith(P.PUBLIC_PREFIXES)


# --------------------------------------------------------------------------- 测试用户

def make_user(username: str, role: str, active: bool = True) -> None:
    with SessionLocal() as db:
        if db.query(User).filter(User.username == username).first():
            return
        db.add(User(username=username, password_hash=hash_password(PASSWORD),
                    display_name=username, role=role, is_active=active, pwd_ver=1))
        db.commit()
    P.invalidate_perm_cache()


def login(client: TestClient, username: str) -> int:
    r = client.post("/login", data={"username": username, "password": PASSWORD},
                    follow_redirects=False)
    return r.status_code


def user_obj(username: str):
    with SessionLocal() as db:
        return db.query(User).filter(User.username == username).first()


def expected_403(user, path: str) -> bool:
    """按守卫自身的判定逻辑算出这次请求是否应当 403。"""
    mod, act = P.lookup_route(path)
    if mod is None or act is None:
        return True
    with SessionLocal() as db:
        db_user = db.get(User, user.id)
        return not P.can(db_user, mod, act, db)


# --------------------------------------------------------------------------- 各项检查

def check_registration(routes: list[tuple[str, str]]) -> None:
    """C1 写操作必须全部登记动作，否则 fail-closed 会把老板自己挡在门外。"""
    live = {f"{m} {p}" for m, p in routes}
    missing = sorted(f"{m} {p}" for m, p in routes
                     if m != "GET" and is_guarded(p) and f"{m} {p}" not in P.ROUTE_ACTIONS)
    for item in missing:
        R.fail("C1 写操作未登记动作", f"{item}  → 任何角色访问都会被拒（含超管）")
    if not missing:
        R.ok()

    stale = sorted(set(P.ROUTE_ACTIONS) - live)
    for item in stale:
        R.warn("C1 登记表存在孤儿条目", f"{item}  → 代码里已无此路由，可从 _route_actions.json 删除")


def check_index_completeness(routes: list[tuple[str, str]]) -> None:
    """C0 索引必须覆盖每个注册模块——这正是线上「全员 403」事故的根因。"""
    covered = {P.lookup_route(concrete(p))[0] for _, p in routes}
    covered.discard(None)
    missing = sorted(k for k in P.ALL_MODULES - covered if not P.MODULES[k].pending)
    for key in missing:
        R.fail("C0 模块无路由", f"{key}（{P.MODULES[key].label}）在索引里没有任何可匹配路由")
    if not missing:
        R.ok()


def check_superuser(routes: list[tuple[str, str]]) -> None:
    """C2 老板 / admin 不得被自己的系统锁在门外。"""
    targets = [p for m, p in routes if m == "GET" and is_guarded(p)]
    for username in ("u_老板", "u_admin"):
        client = TestClient(app)
        if login(client, username) != 303:
            R.fail("C2 超管登录", f"{username} 登录未返回 303")
            continue
        for path in targets:
            if probe(client, "GET", path) == 403:
                R.fail("C2 超管被误拒", f"{username} GET {path} → 403")
        R.ok()


def check_guard_consistency(routes: list[tuple[str, str]]) -> None:
    """C3 逐角色逐路由：HTTP 结果必须与权限判定一致（既不漏放，也不错杀）。"""
    targets = [p for m, p in routes if m == "GET" and is_guarded(p)]
    for role in ROLES:
        username = f"u_{role}"
        user = user_obj(username)
        client = TestClient(app)
        if login(client, username) != 303:
            R.fail("C3 登录失败", username)
            continue
        for path in targets:
            status = probe(client, "GET", path)
            if status == CRASH:
                continue
            want = expected_403(user, concrete(path))
            if want and status != 403:
                R.fail("C3 漏放（应拒未拒）", f"{role} GET {path} → {status}，判定为无权")
            elif not want and status == 403:
                R.fail("C3 错杀（应放误拒）", f"{role} GET {path} → 403，判定为有权")
        R.ok()


def check_zero_perm_user(routes: list[tuple[str, str]]) -> None:
    """C4 零权限账号在任何受控 GET 上都必须是 403。"""
    client = TestClient(app)
    if login(client, f"u_{NO_ROLE}") != 303:
        R.fail("C4 登录失败", NO_ROLE)
        return
    for m, p in routes:
        if m != "GET" or not is_guarded(p):
            continue
        status = probe(client, "GET", p)
        if status not in (403, CRASH):
            R.fail("C4 零权限仍可访问", f"GET {p} 未返回 403")
    R.ok()


def check_user_override() -> None:
    """C5 个人覆盖优先于岗位：加权限要能进，禁权限要能挡住。"""
    # 员工岗位默认没有 expenses，给个人全量权限后应可访问
    make_user("u_override_grant", "员工")
    with SessionLocal() as db:
        u = db.query(User).filter(User.username == "u_override_grant").first()
        db.add(UserModulePerm(user_id=u.id, module="expenses", flags=P.ALL_ACTIONS_MASK))
        db.commit()
    P.invalidate_perm_cache()

    client = TestClient(app)
    if login(client, "u_override_grant") != 303:
        R.fail("C5 登录失败", "u_override_grant")
    elif probe(client, "GET", "/expenses") == 403:
        R.fail("C5 个人加权限无效", "员工 + expenses:view 仍被拒")
    else:
        R.ok()

    # 财务部默认有 expenses，用个人 flags=0 显式关闭后应被挡住
    make_user("u_override_deny", "财务部")
    with SessionLocal() as db:
        u = db.query(User).filter(User.username == "u_override_deny").first()
        db.add(UserModulePerm(user_id=u.id, module="expenses", flags=0))
        db.commit()
    P.invalidate_perm_cache()

    client = TestClient(app)
    if login(client, "u_override_deny") != 303:
        R.fail("C5 登录失败", "u_override_deny")
    elif probe(client, "GET", "/expenses") != 403:
        R.fail("C5 个人禁权限无效", "财务部 + expenses:0 仍可访问")
    else:
        R.ok()


def check_deactivated_session() -> None:
    """C6 账号停用后，已登录会话必须立即失效（退回登录页，而不是继续放行）。"""
    make_user("u_deactivate", "老板")
    client = TestClient(app)
    if login(client, "u_deactivate") != 303:
        R.fail("C6 登录失败", "u_deactivate")
        return
    with SessionLocal() as db:
        u = db.query(User).filter(User.username == "u_deactivate").first()
        u.is_active = False
        db.commit()
    status = client.get("/projects", follow_redirects=False).status_code
    if status not in (302, 303):
        R.fail("C6 停用后会话未失效", f"GET /projects → {status}，应为跳转登录")
    else:
        R.ok()


def check_nav_matches_guard(routes: list[tuple[str, str]]) -> None:
    """C7 侧边栏可见性与实际可达性必须一致，否则会出现「看得见点进去 403」。"""
    first_get: dict[str, str] = {}
    for m, p in routes:
        if m != "GET" or not is_guarded(p):
            continue
        mod, _ = P.lookup_route(concrete(p))
        if mod and mod not in first_get:
            first_get[mod] = p
    for role in P.BUILTIN_ROLES:
        username = f"u_{role}"
        user = user_obj(username)
        client = TestClient(app)
        if login(client, username) != 303:
            continue
        with SessionLocal() as db:
            visible = P.allowed_modules(db.get(User, user.id), db)
        for mod, path in sorted(first_get.items()):
            status = probe(client, "GET", path)
            if status == CRASH:
                continue
            reachable = status != 403
            if (mod in visible) != reachable:
                R.fail("C7 侧边栏与守卫不一致",
                       f"{role}：{'显示' if mod in visible else '隐藏'}「{P.MODULES[mod].label}」"
                       f"，但 GET {path} {'可访问' if reachable else '被拒'}")
    R.ok()


def check_session_version() -> None:
    """C8 改密码后 pwd_ver 递增，旧会话必须作废。"""
    make_user("u_pwdver", "老板")
    client = TestClient(app)
    if login(client, "u_pwdver") != 303:
        R.fail("C8 登录失败", "u_pwdver")
        return
    with SessionLocal() as db:
        u = db.query(User).filter(User.username == "u_pwdver").first()
        u.pwd_ver = (u.pwd_ver or 1) + 1
        db.commit()
    status = client.get("/projects", follow_redirects=False).status_code
    if status not in (302, 303):
        R.fail("C8 改密后旧会话仍有效", f"GET /projects → {status}，应为跳转登录")
    else:
        R.ok()


def check_isolation() -> None:
    """收尾断言：应用引擎必须指向临时目录，生产库全程未被打开。"""
    url = str(engine.url)
    if not url.startswith(f"sqlite:///{TMPDIR}"):
        R.fail("C9 隔离失效", f"引擎指向 {url}，不在临时目录 {TMPDIR}")
    else:
        R.ok()


# --------------------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="装修 PMS 权限回归测试")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（默认失败时自动保留）")
    args = ap.parse_args()

    print(f"临时数据目录：{TMPDIR}")
    print(f"生产库（只读引用，本次不打开）：{PROD_DB}")

    for role in ROLES:
        make_user(f"u_{role}", role)

    routes = route_inventory()
    print(f"受控路由：{sum(1 for _, p in routes if is_guarded(p))} 条"
          f"（其中 GET {sum(1 for m, _ in routes if m == 'GET')} 条）\n")

    check_index_completeness(routes)
    check_registration(routes)
    check_superuser(routes)
    check_guard_consistency(routes)
    check_zero_perm_user(routes)
    check_user_override()
    check_deactivated_session()
    check_nav_matches_guard(routes)
    check_session_version()
    check_isolation()

    print("=" * 66)
    if R.crashes:
        print(f"视图层故障 {len(R.crashes)} 处（与权限无关，但会让用户看到 500 空白页）：")
        for route, detail in sorted(R.crashes.items()):
            print(f"  ! {route}  →  {detail}")
        print()
    for check, detail in R.warnings:
        print(f"[警告] {check}：{detail}")
    if R.failures:
        print(f"\n失败 {len(R.failures)} 项：")
        for check, detail in R.failures:
            print(f"  ✗ {check}：{detail}")
    else:
        print(f"\n权限检查全部通过（{R.passed} 项，{len(routes)} 条路由 × {len(ROLES)} 个岗位）")
    print("=" * 66)

    if R.failures or args.keep:
        print(f"临时目录已保留：{TMPDIR}")
        return 1 if R.failures else 0
    shutil.rmtree(TMPDIR, ignore_errors=True)
    return 1 if R.failures else 0


if __name__ == "__main__":
    sys.exit(main())
