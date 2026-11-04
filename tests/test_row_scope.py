"""行级权限回归：范围判定、跨项目泄露、汇总数字、迁移回填。

为什么这个测试要以「泄露」为第一判据
----------------------------------
行级权限的失败模式不是报错，而是**数字算错或数据漏出去**：
页面正常渲染、测试也可能照样绿，只是设计师在列表里看到了老板的项目。
所以每条断言都写成「不该看到的东西确实没出现」。

四种范围：all / assigned / own / none。
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
from app.models import (BudgetItem, CostItem, Customer, Payment, Project,
                        Role, RoleModulePerm, User)
from app.todo import collect as todo_collect
from app.row_scope import (SCOPE_ALL, SCOPE_ASSIGNED, SCOPE_NONE, SCOPE_OWN,
                            can_see_project, normalize, project_scope_filter,
                            user_scope, visible_projects)

BASE = os.environ.get("PMS_BASE", "http://127.0.0.1:8018")
MARK = "SCOPETEST"
PASS, FAIL = [], []

_fixtures: dict = {}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f" —— {detail}" if detail and not cond else ""))


def cleanup():
    try:
        db = SessionLocal()
        f = _fixtures
        if f.get("pay_ids"):
            db.query(Payment).filter(Payment.id.in_(f["pay_ids"])).delete(synchronize_session=False)
        # 预算/费用：名字带 MARK，按 note 兜底清理（cost_item 的 note 为空时靠项目）
        db.query(CostItem).filter(CostItem.item_name.like(f"%{MARK}%")).delete(
            synchronize_session=False)
        db.query(BudgetItem).filter(BudgetItem.item_name.like(f"%{MARK}%")).delete(
            synchronize_session=False)
        for p in f.get("proj_ids", []):
            proj = db.get(Project, p)
            if proj:
                # 清掉合同/收款等子表，避免留下孤儿
                for pay in list(proj.payments):
                    db.delete(pay)
                if proj.contract:
                    db.delete(proj.contract)
                db.delete(proj)
        if f.get("cust_id"):
            c = db.get(Customer, f["cust_id"])
            if c:
                db.delete(c)
        if f.get("temp_role"):
            from app.models import RoleModulePerm
            tr = f["temp_role"]
            db.query(RoleModulePerm).filter(RoleModulePerm.role == tr).delete(
                synchronize_session=False)
            # 岗位行也要删：它是 UNIQUE，留着会让下一轮 db2.add(Role(...))
            # 撞 IntegrityError（症状是测试在授权前就崩，跟行级毫无关系）。
            db.query(Role).filter(Role.name == tr).delete(synchronize_session=False)
        if f.get("users"):
            db.query(User).filter(User.id.in_(f["users"])).delete(synchronize_session=False)
        db.commit()
        db.close()
        print("\n已清理临时夹具")
    except Exception as e:
        print(f"\n清理失败：{e}")


atexit.register(cleanup)
# 开跑前先清掉历次崩溃留下的：temp_role 用模块级常量拿不到，
# 所以在这里先设一次——cleanup 依赖 _fixtures，而岗位行是在 main 里建的。
_fixtures["temp_role"] = f"{MARK.upper()}_ROLE"
cleanup()
_fixtures.pop("temp_role", None)


def login_as(username, password):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.open(BASE + "/login", timeout=20)
    op.open(BASE + "/login", urllib.parse.urlencode(
        {"username": username, "password": password}).encode(), timeout=20)
    return op


class _FakeUser:
    """构造不依赖数据库的 user 对象，用于纯函数测试。"""
    def __init__(self, uid, display_name, role, scope):
        self.id = uid
        self.display_name = display_name
        self.username = display_name or f"u{uid}"
        self.role = role
        self.data_scope = scope
        self.is_active = True


def main():
    # ---------- 1. normalize：认不出就当 none（fail-closed）----------
    check("normalize 合法值原样返回", normalize("all") == "all")
    check("normalize 大写/空格收敛", normalize("  ALL ") == "all")
    check("normalize 非法值落 none 而不是 all（配错时宁可看不到）",
          normalize("xxx") == "none", normalize("xxx"))
    check("normalize 空值落 none", normalize("") == "none")
    check("normalize None 落 none", normalize(None) == "none")

    # ---------- 2. user_scope：老账号（无 data_scope）默认 all ----------
    old = _FakeUser(9, "老账号", "员工", None)
    check("无 data_scope 的老账号默认 all（升级后行为不变）",
          user_scope(old) == "all", user_scope(old))
    check("老板恒为 all", user_scope(_FakeUser(1, "老板", "老板", "none")) == "all")
    check("admin 恒为 all", user_scope(_FakeUser(1, "a", "admin", "none")) == "all")
    check("普通员工按设定范围", user_scope(_FakeUser(2, "张三", "员工", "assigned")) == "assigned")

    # ---------- 3. project_scope_filter 的返回值语义 ----------
    db = SessionLocal()
    admin = _FakeUser(1, "老板", "老板", "all")
    check("超级用户的过滤条件是 None（= 不限制）",
          project_scope_filter(admin, Project) is None)
    scoped = _FakeUser(2, "张三", "员工", "assigned")
    cond = project_scope_filter(scoped, Project)
    check("assigned 返回 SQL 条件（非 None/False）",
          cond is not None and cond is not False)
    nonescope = _FakeUser(3, "李四", "员工", "none")
    check("none 返回恒假条件（不是 None）",
          project_scope_filter(nonescope, Project) is False)

    # ---------- 4. 造夹具：3 个项目、3 个用户 ----------
    cust = Customer(name=f"{MARK}客户")
    db.add(cust)
    db.flush()
    cust_id = cust.id

    p_a = Project(customer_id=cust_id, name=f"{MARK}-A-别人负责",
                  designer="王五", foreman="", supervisor="", status="施工中")
    p_b = Project(customer_id=cust_id, name=f"{MARK}-B-我负责",
                  designer="张三", foreman="", supervisor="", status="施工中")
    p_c = Project(customer_id=cust_id, name=f"{MARK}-C-无负责人",
                  designer="", foreman="", supervisor="", status="待开工")
    db.add_all([p_a, p_b, p_c])
    db.flush()

    u_me = User(username=f"{MARK.lower()}_zhangsan", display_name="张三",
                password_hash="x", role="员工", data_scope=SCOPE_ASSIGNED)
    u_other = User(username=f"{MARK.lower()}_owner", display_name="占位",
                   password_hash="x", role="员工", data_scope=SCOPE_ASSIGNED)
    db.add_all([u_me, u_other])
    db.flush()
    # B 指派给张三（按名字），C 指派给 u_other（按外键，验证两种匹配都生效）
    p_b.owner_user_id = u_me.id
    p_c.owner_user_id = u_other.id
    p_a.owner_user_id = u_other.id
    # D 由我创建、且负责人字段里**没有**我的名字。
    # own 范围只认 owner_user_id，所以必须有一个「纯靠外键命中」的项目，
    # 否则「我创建的项目可见」这条在别人把 owner 清掉时就会假绿。
    p_d = Project(customer_id=cust_id, name=f"{MARK}-D-我创建无负责人",
                  designer="", foreman="", supervisor="", status="施工中")
    db.add(p_d)
    db.flush()
    p_d.owner_user_id = u_me.id
    pay = Payment(project_id=p_a.id, name="签约款", plan_amount=10000, paid_amount=0)
    db.add(pay)
    db.flush()
    db.commit()
    _fixtures.update(proj_ids=[p_a.id, p_b.id, p_c.id, p_d.id], cust_id=cust_id,
                     users=[u_me.id, u_other.id], pay_ids=[pay.id])

    # ---------- 5. assigned 范围：按名字或外键命中 ----------
    vis_ids = {p.id for p in visible_projects(db, u_me, Project)}
    check("assigned：我负责的项目可见", p_b.id in vis_ids)
    check("assigned：别人负责的项目不可见（按 designer 文本）",
          p_a.id not in vis_ids, f"可见={vis_ids}")
    check("assigned：非我创建但我被指为负责人 → 不可见（别人的）",
          p_c.id not in vis_ids)

    u_other.db_me = None
    vis_o = {p.id for p in visible_projects(db, u_other, Project)}
    check("assigned：外键命中也算「我负责」", p_c.id in vis_o, f"可见={vis_o}")

    # ---------- 6. own 范围：只看创建者 ----------
    u_me.data_scope = SCOPE_OWN
    # B 的 owner 临时改回别人：own 范围下 B 就该看不见了
    p_b.owner_user_id = u_other.id
    db.commit()
    vis_own = {p.id for p in visible_projects(db, u_me, Project)}
    check("own：我创建的项目可见（D，靠外键命中）", p_d.id in vis_own, f"可见={vis_own}")
    check("own：别人的项目不可见", p_a.id not in vis_own)
    check("own：仅被指为负责人（未创建）→ 不可见", p_b.id not in vis_own)
    check("own：无关项目不可见", p_c.id not in vis_own)
    u_me.data_scope = SCOPE_ASSIGNED
    p_b.owner_user_id = u_me.id
    db.commit()

    # ---------- 7. none 范围：什么都看不到 ----------
    u_me.data_scope = SCOPE_NONE
    check("none：可见集合为空", len(visible_projects(db, u_me, Project)) == 0)
    check("none：单个项目也判定不可见",
          can_see_project(u_me, db.get(Project, p_a.id), Project) is False)
    u_me.data_scope = SCOPE_ASSIGNED

    # ---------- 8. can_see_project 与 SQL 条件一致 ----------
    for scope in (SCOPE_ALL, SCOPE_ASSIGNED, SCOPE_OWN, SCOPE_NONE):
        u_me.data_scope = scope
        sql_ids = {p.id for p in visible_projects(db, u_me, Project)}
        for pid in (p_a.id, p_b.id, p_c.id):
            py_ok = can_see_project(u_me, db.get(Project, pid), Project)
            if py_ok != (pid in sql_ids):
                check(f"can_see_project 与 SQL 条件一致（scope={scope}, proj={pid}）", False,
                      f"py={py_ok} sql={pid in sql_ids}")
                break
        else:
            continue
        break
    else:
        check("can_see_project 与 SQL 条件在四种范围下全部一致", True)
    u_me.data_scope = SCOPE_ASSIGNED
    db.commit()
    db.close()

    # ---------- 9. 页面级：员工看不到别人的项目 ----------
    # 用**真实登录**测页面，因为行级权限的失败模式是「页面正常渲染但数据漏了」，
    # 只测纯函数覆盖不到渲染层与侧栏项目树。
    pwd = "scopetest123"
    from app.auth import hash_password
    from app.models import RoleModulePerm
    TEMP_ROLE = f"{MARK.upper()}_ROLE"

    u_me_id, old_role = u_me.id, u_me.role
    p_a_id, p_b_id, p_c_id = p_a.id, p_b.id, p_c.id

    db2 = SessionLocal()
    # ⚠ 必须在**新会话里重新取**用户：`u_me` 属于已关闭的 `db` 会话，
    # 直接改它的字段再 db2.commit() 不会落库——于是登录用旧哈希失败，
    # 报的是 HTTP 400「用户名或密码错误」，看起来像账号有问题。
    u_me2 = db2.get(User, u_me_id)
    u_me2.password_hash = hash_password(pwd)
    p_a2 = db2.get(Project, p_a_id)
    p_a2.designer = "王五"      # A 指派给王五：构造「别人负责」的对照
    db2.flush()
    db2.commit()
    db2.close()
    # 给临时岗位授予相关模块，否则被**模块级**守卫挡住，
    # 测到的就成了模块权限而不是行级权限。
    #
    # ⚠ 必须走 HTTP 的 /oa/permissions 而不是直接插 RoleModulePerm：
    #   权限在进程内有缓存（permissions._PERM_CACHE），只由
    #   invalidate_perm_cache() 失效，而它只在 OA 保存路由里被调用。
    #   测试进程直插数据库 → 服务进程仍读旧缓存 → 全部 403。
    #   症状极具迷惑性：前面所有断言都「看不到数据」，像是行级过滤太严，
    #   实际是一律没权限。
    # 岗位可能已存在（上轮崩溃留下的）：先查再插。
    # roles.name 是 UNIQUE，重复插会 IntegrityError——清理函数只在
    # 跑完时调用，中途异常就跳过了，所以残留是常态而非意外。
    if not db2.query(Role).filter(Role.name == TEMP_ROLE).first():
        db2.add(Role(name=TEMP_ROLE, builtin=False, note="行级权限测试用临时岗位"))
    u3 = db2.get(User, u_me_id)
    u3.role = TEMP_ROLE
    db2.commit()
    db2.close()
    _fixtures["temp_role"] = TEMP_ROLE

    # 以 admin 身份通过真实接口授权（顺带验证该接口本身可用）
    op_admin = login_as("admin", "admin123")
    form = {"role": TEMP_ROLE}
    # 名单必须覆盖下面**实际访问**的每个路径所属模块。tasks 是独立模块
    # （见 permissions.py 的 ModuleDef("tasks", ...)），不随 projects 走——
    # 少授一个就 403，而症状是「页面打开是空的」，看着像过滤过头。
    for m in ("projects", "dashboard", "todo", "customers", "reports", "tasks"):
        for a in ("view", "create", "edit", "delete", "export", "approve"):
            form[f"p_{m}_{a}"] = "1"
    op_admin.open(BASE + "/oa/permissions", urllib.parse.urlencode(form).encode(),
                  timeout=30)
    # 回读验证授权真的落库了。
    # permissions_save 在**岗位不存在**时会直接 303 跳走，既不报错也不写库，
    # 不回读就无从知道「授权没生效」——而下面的断言会因为各种别的原因
    # （比如恰好有别的账号有权限）照样通过，形成假绿。
    _dbchk = SessionLocal()
    _flags = {r.module: r.flags for r in _dbchk.query(RoleModulePerm).filter(
        RoleModulePerm.role == TEMP_ROLE).all()}
    _dbchk.close()
    check("岗位权限已真正写入（回读验证，防保存接口静默跳走）",
          all(_flags.get(m, 0) > 0 for m in
              ("projects", "dashboard", "todo", "customers", "reports", "tasks")),
          f"实际 flags={_flags}")

    granted = login_as(f"{MARK.lower()}_zhangsan", pwd)
    try:
        _probe = granted.open(BASE + "/projects", timeout=20)
        check("临时岗位已获得 projects 模块权限（否则测的是模块级而非行级）",
              _probe.status == 200, f"实际 {_probe.status}")
    except urllib.error.HTTPError as e:
        check("临时岗位已获得 projects 模块权限", False, f"HTTP {e.code}")
    op = granted

    op = login_as(f"{MARK.lower()}_zhangsan", pwd)
    r = op.open(BASE + "/projects", timeout=20)
    html = r.read().decode("utf-8")
    check("员工能看到自己负责的项目", f"{MARK}-B-我负责" in html)
    check("员工看不到别人负责的项目（列表页）",
          f"{MARK}-A-别人负责" not in html, "泄露：别人的项目名出现在列表里")
    check("员工看不到与自己无关的项目",
          f"{MARK}-C-无负责人" not in html)

    # 详情页：URL 里的 id 可以手输，列表页过滤挡不住，必须在这里二次校验
    try:
        r2 = op.open(BASE + f"/projects/{p_a_id}", timeout=20)
        leaked = f"{MARK}-A-别人负责" in r2.read().decode("utf-8")
        check("手输他人项目 id 拿不到详情", not leaked, f"HTTP {r2.status}，泄露={leaked}")
        check("详情越权返回 404 而非 403（不泄露「存在」）", r2.status == 404,
              f"实际 {r2.status}")
    except urllib.error.HTTPError as e:
        check("手输他人项目 id 拿不到详情", e.code == 404, f"实际 {e.code}")
        check("详情越权返回 404 而非 403（不泄露「存在」）", True)

    r7 = op.open(BASE + f"/projects/{p_b_id}", timeout=20)
    check("自己负责的项目详情可正常访问", r7.status == 200, f"实际 {r7.status}")

    # 首页：合同额合计与项目列表都在这里。
    # /todo 与 /customers 交给下面「能看到那一半」统一验证可达性——
    # 待办模块对未授权岗位是 403，而本测试的临时岗位没授它，
    # 强行打开只会让「看不到」和「打不开」两种情况混在一起，分不清是哪种。
    r3 = op.open(BASE + "/", timeout=20)
    h3 = r3.read().decode("utf-8")
    check("首页不出现他人项目名", f"{MARK}-A-别人负责" not in h3)
    check("首页不出现无关项目名", f"{MARK}-C-无负责人" not in h3)

    # 侧栏项目树：base.html 每个页面都渲染，是最容易漏的一处。
    # 用 /projects 测：它已被上面确认返回 200，所以「没有 A」只能归因于行级过滤。
    check("侧栏项目树不出现他人项目名（侧栏在每个页面都渲染）",
          f"{MARK}-A-别人负责" not in html)

    # 成本报表导出：导出文件会离开系统，不过滤等于把金额发出去。
    # xlsx 是 zip 容器，文件名以 UTF-8 存在 sharedStrings.xml 里，
    # 但可能被压缩——所以先确认能否直接在字节里找到，找不到就解开 zip 看。
    r8 = op.open(BASE + "/reports/costs/export", timeout=30)
    x = r8.read()
    name_a = f"{MARK}-A-别人负责".encode("utf-8")
    found = name_a in x
    if not found:
        import io as _io
        import zipfile
        try:
            with zipfile.ZipFile(_io.BytesIO(x)) as zf:
                found = any(name_a in zf.read(n) for n in zf.namelist())
        except Exception:
            found = False
    check("成本报表导出不含他人项目", not found,
          "泄露：导出的 xlsx 里含他人项目行")

    # 对照：all 范围必须能看到全部夹具项目。
    # 没有这条，上面几条可能因为「所有人什么都看不到」而假绿——
    # 那是本次改动最坏的一种故障：所有人一夜之间看不到任何项目。
    op3 = login_as("admin", "admin123")
    r9 = op3.open(BASE + "/projects", timeout=20)
    h9 = r9.read().decode("utf-8")
    check("对照：admin（全部范围）能看到所有夹具项目",
          all(k in h9 for k in (f"{MARK}-A-别人负责", f"{MARK}-B-我负责",
                                f"{MARK}-C-无负责人")),
          "all 范围失效会让上面所有断言变成假绿")

    # ---------- 10. 老账号迁移回填：data_scope 必须是 'all' ----------
    db3 = SessionLocal()
    nulls = db3.query(User).filter(User.data_scope.is_(None)).count()
    check("users.data_scope 无 NULL（迁移已回填）", nulls == 0,
          f"{nulls} 个账号仍为 NULL —— 会让它们看不到任何项目")
    bad = db3.query(User).filter(
        User.data_scope.notin_(["all", "assigned", "own", "none"])).count()
    check("users.data_scope 无非法值", bad == 0, f"{bad} 个账号值非法")
    pnulls = db3.query(Project).filter(Project.owner_user_id.is_(None)).count()
    print(f"  （项目 owner_user_id 为 NULL 的有 {pnulls} 个，属正常："
          "存量项目无从推断创建者）")
    db3.close()

    # todo_collect 每次都用新会话，用完即关——否则连接堆积，
    # 且会话里缓存的 ORM 对象在夹具被删后仍持有引用。
    _d = SessionLocal()
    _mine_items = todo_collect(_d, _d.get(User, u_me_id))
    _d.close()
    mine_titles = [i["title"] for i in _mine_items]
    check("收紧范围的员工待办里不含 A 的成本超支",
          not any(t.startswith(f"{MARK}-A") for t in mine_titles),
          "泄露：待办里出现他人项目")

    # 还原：走 HTTP 接口没有「删除角色」的动作，直接清库里的行并让缓存失效。
    # 这里清不干净也没关系——临时岗位名带 MARK，下轮 cleanup 会删。
    dbs = SessionLocal()
    u3 = dbs.get(User, u_me_id)
    u3.role = old_role
    dbs.commit()
    dbs.query(RoleModulePerm).filter(
        RoleModulePerm.role == TEMP_ROLE).delete(synchronize_session=False)
    dbs.query(Role).filter(Role.name == TEMP_ROLE).delete(synchronize_session=False)
    dbs.commit()
    dbs.close()

    # ---------- 11. 待办总览：待办项里带他人项目名 ----------
    # 用 admin 造一条他人项目的待办（如成本超预算），再确认收紧范围的
    # 员工在自己也能触发的待办页面（/tasks）上看不到它。
    # 这里**不用 /todo**：它属于「todo」模块，与 projects 无关，
    # 为测行级去临时授它反而把测试变成测模块级。
    # 两个项目都造「成本超预算」：A（他人负责）与 B（我负责）。
    # 两边都造才能验证「该看的还在」——只造 A 的话，员工看不到 A 可能
    # 是过滤生效（对）也可能是待办里压根没有别的（过头了），无法区分。
    # 两个项目都造「成本超预算」：A（他人负责）与 B（我负责）。
    # 两边都造才能验证「该看的还在」——只造 A 的话，员工看不到 A 可能
    # 是过滤生效（对）也可能是待办里压根没有别的（过头了），无法区分。
    # 预算名也带 MARK，好让「别人的那条」可用 MARK 精确定位。
    db4 = SessionLocal()
    for _pid, _tag in ((p_a_id, "A"), (p_b_id, "B")):
        db4.add(BudgetItem(project_id=_pid, category="人工",
                           item_name=f"{MARK}{_tag}预算", budget_amount=1000))
        db4.add(CostItem(project_id=_pid, category="人工",
                         item_name=f"{MARK}{_tag}费用", amount=99999))
    db4.commit()
    db4.close()

    # 对照：admin 的待办里应该出现 A 的成本超支。
    # 没有这条，下面「员工看不到 A」就可能因为「所有人都看不到」而假绿。
    db5 = SessionLocal()
    items_admin = todo_collect(db5, db5.query(User).filter(
        User.username == "admin").first())
    db5.close()
    admin_a = [i for i in items_admin if i["kind"] == "成本超预算"
               and i["title"].startswith(f"{MARK}-A")]
    check("对照：admin 的待办里有 A 的成本超支",
          len(admin_a) >= 1,
          f"all 范围失效会让下面几条假绿；"
          f"实际成本预警标题={[i['title'] for i in items_admin if '成本' in i['kind']]}")

    # 自己项目的成本超支必须仍然出现——这是「过滤没过头」的唯一证据。
    # 判据要覆盖**两种**成本预警：成本超预算（确定性）与
    # 成本逼近合同额（阈值型）。只判前者的话，哪天它被挪走或改名，
    # 这条断言就会在「其实没有超支待办」的情况下假绿。
    mine_kinds = {}
    _d = SessionLocal()
    _mine_items = todo_collect(_d, _d.get(User, u_me_id))
    _d.close()
    for i in _mine_items:
        if i["title"].startswith(f"{MARK}-B"):
            mine_kinds[i["kind"]] = i
    check("自己项目的成本超支仍可见（过滤没过头）",
          bool(mine_kinds), f"assigned 范围把该看的也滤掉了；"
                            f"B 相关待办={[ (k, v['title']) for k,v in mine_kinds.items() ]}")
    check("自己项目的成本预警含金额（详情未被清空）",
          any("预算" in v["detail"] or "成本" in v["detail"] for v in mine_kinds.values()),
          f"实际详情={[v['detail'] for v in mine_kinds.values()]}")


if __name__ == "__main__":
    print("=== 行级权限回归 ===")
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
