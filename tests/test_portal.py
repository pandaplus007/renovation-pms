"""业主只读链接回归：暴露面、吊销、过期、越权。

为什么判据是「不该出现的东西没出现」
------------------------------------
这是个**免登录**的公网入口。它的失败模式不是报错，而是
「业主看到了本不该看的数字」或者「以为停用了其实没停」。
所以断言全部写成两极：

  1. 关掉开关 → 页面上**一个字都不能出现**相关字段
  2. 开着开关 → 必须出现（否则是「开关失灵」，同样严重）

只有第 1 类断言是假绿的温床：字段压根没接进模板，也会「通过」。
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
from app.models import (Contract, CostItem, Customer, Payment, Project,
                        ProjectPhase, ProjectPortalLink, Role, RoleModulePerm,
                        SiteRecord, User)
from app.portal import (build_view, find_by_token, get_link, new_token,
                        normalize_token)

BASE = os.environ.get("PMS_BASE", "http://127.0.0.1:8018")
MARK = "PORTALTEST"
PASS, FAIL = [], []
_f: dict = {}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (f" —— {detail}" if detail and not cond else ""))


def cleanup():
    """清理夹具。

    ⚠ 主体按**名字前缀**扫，而不是只删 _f 里记的 id：
      _f["proj_ids"] 是在 main() 末尾才写的，中途任何异常都会让它为空，
      于是那次跑留下的项目就永远删不掉——几轮下来本地库里堆着
      几十个叫「PORTALTEST-业主查看项目」的僵尸项目，
      而它们进了所有统计口径（合同额、客户数、首页计数），
      把真实数据搅成一团。名字是常量，开跑前扫一遍最可靠。
    """
    try:
        db = SessionLocal()
        leftovers = db.query(Project).filter(
            Project.name.like(f"{MARK}%")).all()
        if leftovers:
            print(f"\n清理残留项目 {len(leftovers)} 个")
        for p in leftovers + [db.get(Project, i) for i in _f.get("proj_ids", [])]:
            if not p:
                continue
            # 先删依赖 project_id 的行：有些表没配 cascade，
            # 留着它们会让 projects 的 id 被后续插入复用，造成串数据。
            db.query(ProjectPortalLink).filter(
                ProjectPortalLink.project_id == p.id).delete(
                synchronize_session=False)
            for pay in list(p.payments):
                db.delete(pay)
            for ci in list(p.cost_items):
                db.delete(ci)
            for bi in list(p.budget_items):
                db.delete(bi)
            if p.contract:
                db.delete(p.contract)
            db.delete(p)
        # SiteRecord 用 ORM 逐条删，不走 bulk delete：
        # 它有 followups 关系带 delete-orphan 级联，bulk delete 会触发
        # SAWarning（"expected to delete 2 rows; 0 matched"）——
        # 那只是噪音，但会混进测试输出，让人以为真的删错了行数。
        for sr in db.query(SiteRecord).filter(
                SiteRecord.content.like(f"%{MARK}%")).all():
            db.delete(sr)
        for c in db.query(Customer).filter(
                Customer.name.like(f"%{MARK}%")).all():
            db.delete(c)
        if _f.get("users"):
            db.query(User).filter(User.id.in_(_f["users"])).delete(
                synchronize_session=False)
        db.query(User).filter(User.username.like(f"{MARK.lower()}%")).delete(
            synchronize_session=False)
        if _f.get("temp_role"):
            tr = _f["temp_role"]
            db.query(RoleModulePerm).filter(RoleModulePerm.role == tr).delete(
                synchronize_session=False)
            db.query(Role).filter(Role.name == tr).delete(synchronize_session=False)
        db.commit()
        db.close()
        print("已清理临时夹具")
    except Exception as e:
        import traceback
        print(f"\n清理失败：{e}")
        traceback.print_exc()


atexit.register(cleanup)
# 开跑前先清一次历次崩溃留下的：temp_role 是模块级常量，main() 里才算得出来，
# 这里先塞一次，cleanup 才认得。
_f["temp_role"] = f"{MARK}_ROLE"
cleanup()


def login_as(username, password):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.open(BASE + "/login", timeout=20)
    op.open(BASE + "/login", urllib.parse.urlencode(
        {"username": username, "password": password}).encode(), timeout=20)
    return op


def get(op, path, timeout=20):
    """GET 并返回 (status, html)。非 2xx 不抛，直接给状态码。"""
    try:
        r = op.open(BASE + path, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def post(op, path, data, timeout=20):
    try:
        r = op.open(BASE + path, urllib.parse.urlencode(data).encode(), timeout=timeout)
        return r.status, r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, ""


def main():
    # ================= 1. token 生成的性质 =================
    toks = [new_token() for _ in range(200)]
    check("token 互不相同", len(set(toks)) == 200)
    check("token 有 pmt 前缀（便于人工识别）",
          all(t.startswith("pmt-") for t in toks))
    # 熵够不够：192 位的话，不同前缀的碰撞概率可以忽略。
    lens = {len(t) for t in toks}
    check("token 长度一致（>=32 字符才够随机）", len(lens) == 1 and min(lens) >= 32,
          f"长度集合={sorted(lens)}")
    check("normalize_token 去空白", normalize_token("  pmt-abc  ") == "pmt-abc")
    check("normalize_token 对 None 安全", normalize_token(None) == "")

    db = SessionLocal()

    # ================= 2. 夹具 =================
    cust = Customer(name=f"{MARK}客户", contact_phone="13800000000")
    db.add(cust)
    db.flush()
    proj = Project(customer_id=cust.id, name=f"{MARK}-业主查看项目",
                   address="测试路 1 号", area="120", style="现代简约",
                   status="施工中", designer="张设计师",
                   foreman="李工长", supervisor="")
    db.add(proj)
    db.flush()
    pid = proj.id

    # 合同与成本：这两项**绝不能**出现在业主页面
    db.add(Contract(project_id=pid, amount=888888, sign_date=None))
    db.add(CostItem(project_id=pid, category="人工", item_name="内部成本", amount=555555))
    # 收款节点：开「收款进度」时能看到汇总，但看不到单价
    db.add(Payment(project_id=pid, name="签约款", plan_amount=200000, paid_amount=100000))
    db.add(Payment(project_id=pid, name="中期款", plan_amount=288888, paid_amount=0))
    # 阶段
    for i, (nm, st) in enumerate([("拆除", "已完成"), ("水电", "施工中"),
                                  ("泥木", "待开始")]):
        db.add(ProjectPhase(project_id=pid, name=nm, status=st, sort_order=i))
    # 现场记录：只有「施工照片」该给业主看
    db.add(SiteRecord(project_id=pid, record_type="施工照片", content=f"{MARK}照片",
                      image_path="/uploads/demo.jpg", record_date=None))
    db.add(SiteRecord(project_id=pid, record_type="问题", content=f"{MARK}漏水",
                      image_path="/uploads/leak.jpg", record_date=None))
    db.commit()

    # ================= 3. 默认状态必须是最窄的 =================
    link = get_link(db, pid)
    db.commit()
    check("新建链接默认停用", link.enabled is False)
    check("新建链接默认不显示照片", link.show_photos is False)
    check("新建链接默认不显示收款", link.show_payment is False)

    st, html = get(urllib.request.build_opener(), f"/p/{link.token}")
    check("停用状态下链接打不开（404）", st == 404, f"实际 {st}")
    check("无效页不泄露项目名", f"{MARK}-业主查看项目" not in html)
    check("无效页不提示「已过期」等可区分的原因",
          ("过期" not in html) and ("不存在" not in html))

    # token 猜不到：随机 token 不该命中任何项目
    check("随机 token 不命中", find_by_token(db, "pmt-zzzzzzzzzzzz") is None)

    # ================= 4. get_link 的幂等性 =================
    # 这是 GET 管理页走的路径：重复调用不能改设置。
    link.enabled = True
    link.show_payment = True
    db.commit()
    tok_before = link.token
    again = get_link(db, pid)
    db.commit()
    check("get_link 重复调用保留 enabled（GET 打开页面不会关掉链接）",
          again.enabled is True)
    check("get_link 重复调用保留 show_payment",
          again.show_payment is True)
    check("get_link 重复调用保留 token（不会每次打开都换链接）",
          again.token == tok_before)

    # ================= 5. build_view 的暴露面 =================
    # 默认（无照片/无收款）下最小可见集
    link.show_photos = False
    link.show_payment = False
    db.commit()
    v = build_view(db, proj, link)
    check("默认视图有项目名", v["project_name"] == f"{MARK}-业主查看项目")
    check("默认视图有阶段进度", v["phase_total"] == 3 and v["phase_done"] == 1)
    check("默认视图进度百分比正确（1/3=33%）", v["phase_pct"] == 33, str(v["phase_pct"]))
    check("默认视图**不含**照片", "photos" not in v)
    check("默认视图**不含**收款", "payment" not in v)

    # ⚠ 最关键的一条：视图里绝不能出现内部金额
    blob = repr(v)
    check("视图不含合同额 888888", "888888" not in blob)
    check("视图不含成本 555555", "555555" not in blob)
    check("视图不含客户电话", "13800000000" not in blob)
    check("视图不含负责人姓名（工长/设计）",
          ("张设计师" not in blob) and ("李工长" not in blob))

    # 开照片
    link.show_photos = True
    db.commit()
    v = build_view(db, proj, link)
    check("开照片后有照片列表", "photos" in v and len(v["photos"]) == 1,
          f"实际 {v.get('photos')}")
    check("照片里只有「施工照片」，不含「问题」记录（漏水照片不该给业主看）",
          v["photos"] and all("leak" not in str(p["url"]) for p in v["photos"]))
    check("开照片但未开收款时，收款仍然不出现（两个开关独立）",
          "payment" not in v)

    # 只开收款（照片关）——这是最容易写错的组合：
    # 若 build_view 里写成「照片关了就 return」，收款会跟着一起消失。
    link.show_photos = False
    link.show_payment = True
    db.commit()
    v = build_view(db, proj, link)
    check("**只**开收款（照片关）时收款正常出现", "payment" in v,
          "两个开关不独立 —— 照片关掉会连收款一起吞掉")
    check("只开收款时照片不出现", "photos" not in v)
    check("收款汇总正确（已付 100000）", v["payment"]["paid"] == 100000,
          str(v["payment"]))
    # 总计划 200000 + 288888 = 488888，已付 100000 → 待付 388888
    check("收款待付正确（488888-100000=388888）", v["payment"]["unpaid"] == 388888,
          str(v["payment"]["unpaid"]))
    check("收款百分比正确（约 20%）", v["payment"]["pct"] == 20,
          str(v["payment"].get("pct")))

    # 作废节点不进业主页面：作废 = 这笔钱不算数，给业主看会造成对账纠纷
    pays = db.query(Payment).filter(Payment.project_id == pid).all()
    # ⚠ Payment.status 是 @property（未收/部分/已收清），没有 setter；
    #   作废列在 Python 侧叫 void_status（DB 列名才是 status）。
    #   写 pays[1].status = "作废" 会报
    #   「property 'status' has no setter」，看着像模型坏了。
    pays[1].void_status = "作废"
    db.commit()
    v = build_view(db, proj, link)
    check("作废的收款节点不进业主页面", len(v["payment"]["nodes"]) == 1,
          f"实际节点数={len(v['payment']['nodes'])}")
    check("作废后业主看到的待付金额随之变化", v["payment"]["unpaid"] == 100000,
          str(v["payment"]["unpaid"]))
    check("作废后总额随之变化", v["payment"]["plan"] == 200000,
          str(v["payment"]["plan"]))
    pays[1].void_status = "正常"
    db.commit()

    # ================= 6. 过期 =================
    from datetime import datetime, timedelta
    link.expires_at = datetime.now() - timedelta(days=1)
    db.commit()
    check("过期后 find_by_token 返回 None（哪怕 enabled=True）",
          find_by_token(db, link.token) is None)
    st, _ = get(urllib.request.build_opener(), f"/p/{link.token}")
    check("过期后链接 404", st == 404, f"实际 {st}")
    link.expires_at = datetime.now() + timedelta(days=30)
    db.commit()
    st, html = get(urllib.request.build_opener(), f"/p/{link.token}")
    check("未过期且启用时可正常打开", st == 200, f"实际 {st}")

    # ================= 7. 公开页的实际内容 =================
    opn = urllib.request.build_opener()
    link.show_payment = True
    link.show_photos = True
    db.commit()
    st, html = get(opn, f"/p/{link.token}")
    check("公开页能看到项目名", f"{MARK}-业主查看项目" in html)
    check("公开页能看到阶段", "拆除" in html and "水电" in html)
    check("公开页**看不到**合同额", "888888" not in html)
    check("公开页**看不到**成本", "555555" not in html)
    check("公开页**看不到**客户电话", "13800000000" not in html)
    check("公开页**看不到**负责人姓名",
          ("张设计师" not in html) and ("李工长" not in html))
    check("公开页**看不到**问题记录照片", "leak.jpg" not in html)
    check("公开页看得到施工照片", "demo.jpg" in html)
    check("公开页不出现内部导航/登录框（不该继承 base.html）",
          ("/logout" not in html) and ("项目列表" not in html))
    check("公开页带 no-store（停用后不能从缓存继续看）",
          "no-store" in _headers_of(f"/p/{link.token}"))
    check("公开页带 noindex（不被搜索引擎收录）",
          "noindex" in html.lower() or "noindex" in _headers_of(f"/p/{link.token}"))

    # 访问计数。
    # ⚠ 上面那几条断言里 `_headers_of()` 自己就访问了两次页面，
    #   所以计数基线必须**在它们之后**取，否则这里会凭空差 2 次，
    #   让人以为计数逻辑坏了。
    db.expire_all()
    before = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first().view_count or 0
    get(opn, f"/p/{link.token}")
    db.expire_all()
    link = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first()
    check("访问被计数（一次请求 +1）", (link.view_count or 0) == before + 1,
          f"{before} -> {link.view_count}")
    check("记录了最近访问时间", link.last_viewed_at is not None)

    # ================= 8. 停用 =================
    link.enabled = False
    db.commit()
    st, html = get(opn, f"/p/{link.token}")
    check("停用后立刻 404", st == 404, f"实际 {st}")
    check("停用后页面不含项目名", f"{MARK}-业主查看项目" not in html)
    check("停用后 find_by_token 也返回 None",
          find_by_token(db, link.token) is None)

    # ================= 9. 管理页：行级权限 =================
    # 造一个 assigned 范围的员工，让他对**自己看不到**的项目打开管理页。
    #
    # ⚠ 授权必须走 HTTP 的 /oa/permissions，不能直插 RoleModulePerm：
    #   权限在服务进程内有缓存（permissions._PERM_CACHE），
    #   只由 invalidate_perm_cache() 失效，而它只在 OA 保存路由里被调用。
    #   直插数据库 → 服务进程读旧缓存 → 全部 403，
    #   症状极具迷惑性：看起来像「行级权限把所有人挡在外面」。
    #
    # ⚠⚠ 顺序必须是「先建岗位，再授权」。
    #   permissions_save 第 203 行：岗位不存在时直接 303 跳走，
    #   **不报错、不写库、不打日志**。
    #   反过来做（先授权后建岗位）会静默无效，
    #   而「员工能打开页面」这个断言可能因为**上一轮遗留的岗位恰好在服务
    #   启动时被 _seed_permissions() 播了全量权限**而通过——
    #   本测试此前就是这样假绿的，直到有次重启服务把遗留岗位清掉才暴露。
    from app.auth import hash_password
    TEMP_ROLE = f"{MARK}_ROLE"
    pwd = "portaltest123"

    if not db.query(Role).filter(Role.name == TEMP_ROLE).first():
        db.add(Role(name=TEMP_ROLE, builtin=False, note="业主链接测试用临时岗位"))
    db.commit()

    opa0 = login_as("admin", "admin123")
    form = {"role": TEMP_ROLE}
    # dashboard 必须一起授：登录成功后 302 跳到 `/`，
    # 而 `/` 属于 dashboard 模块。只授 projects 的话登录本身就会 403，
    # 报错指向「登录失败 / 密码错误」，与真正的根因（岗位权限不全）毫无关系。
    for m in ("projects", "dashboard"):
        for a in ("view", "create", "edit", "delete", "export", "approve"):
            form[f"p_{m}_{a}"] = "1"
    opa0.open(BASE + "/oa/permissions", urllib.parse.urlencode(form).encode(),
              timeout=30)

    # 授权结果必须**回读验证**。这一步不是冗余：
    # 上面的保存接口在岗位不存在时静默跳走，不回读就永远不知道它没生效。
    db.expire_all()
    granted_flags = {r.module: r.flags for r in db.query(RoleModulePerm).filter(
        RoleModulePerm.role == TEMP_ROLE).all()}
    check("岗位权限已真正写入（回读验证，防保存接口静默跳走）",
          granted_flags.get("projects", 0) > 0
          and granted_flags.get("dashboard", 0) > 0,
          f"projects={granted_flags.get('projects')} "
          f"dashboard={granted_flags.get('dashboard')}")

    emp = db.query(User).filter(User.username == f"{MARK.lower()}_emp").first()
    if emp is None:
        emp = User(username=f"{MARK.lower()}_emp", display_name="王五",
                   password_hash=hash_password(pwd), role=TEMP_ROLE,
                   data_scope="assigned")
        db.add(emp)
        db.commit()
    else:
        emp.password_hash = hash_password(pwd)
        emp.data_scope = "assigned"
        emp.role = TEMP_ROLE
        db.commit()
    _f["users"] = [emp.id]
    _f["temp_role"] = TEMP_ROLE

    op = login_as(f"{MARK.lower()}_emp", pwd)
    # 先确认模块权限确实授到了（否则下面几条 404 是模块级造成的假绿）
    st, ph = get(op, "/projects")
    check("临时员工能打开项目列表（模块权限已授到位）", st == 200, f"实际 {st}")
    check("临时员工看不到该夹具项目（列表已按范围收窄）",
          f"{MARK}-业主查看项目" not in ph)
    # 这个项目的设计师是「张设计师」，员工是「王五」，assigned 范围看不到
    st, _ = get(op, f"/projects/{pid}/portal")
    check("行级范围外的员工打不开业主链接管理页（404 而非 403）",
          st == 404, f"实际 {st}")
    st, _ = post(op, f"/projects/{pid}/portal", {"enabled": "on"})
    check("越权 POST 同样被拒（403/404）", st in (403, 404), f"实际 {st}")
    db.expire_all()
    link = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first()
    check("越权 POST 没有改变链接状态", link.enabled is False)
    st, _ = post(op, f"/projects/{pid}/portal/reset", {})
    db.expire_all()
    link2 = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first()
    check("越权 reset 没有换掉 token", link2.token == link.token)

    # ================= 10. 管理页：功能与吊销 =================
    opa = login_as("admin", "admin123")
    st, html = get(opa, f"/projects/{pid}/portal")
    check("admin 能打开管理页", st == 200, f"实际 {st}")
    check("管理页显示项目名", f"{MARK}-业主查看项目" in html)

    # 保存：启用 + 收款
    st, url = post(opa, f"/projects/{pid}/portal",
                   {"enabled": "on", "show_payment": "on", "expire_days": "90"})
    check("保存后跳转回管理页", st == 200 or st == 303, f"实际 {st}")
    db.expire_all()
    link = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first()
    check("保存后 enabled=True", link.enabled is True)
    check("保存后 show_payment=True", link.show_payment is True)
    check("保存后 show_photos 保持关闭（未提交的开关不被打开）",
          link.show_photos is False)
    tok_after_save = link.token
    st, _ = get(opn, f"/p/{tok_after_save}")
    check("保存后业主链接可用", st == 200, f"实际 {st}")

    # 有效期上限：1095 天
    post(opa, f"/projects/{pid}/portal",
         {"enabled": "on", "show_payment": "on", "expire_days": "99999"})
    db.expire_all()
    link = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first()
    days = (link.expires_at - link.created_at).days if link.expires_at else -1
    check("有效期被截断到 ≤3 年（防「永久有效」）",
          link.expires_at is not None and days <= 1095 + 1, f"{days} 天")
    # 非法输入
    post(opa, f"/projects/{pid}/portal",
         {"enabled": "on", "show_payment": "on", "expire_days": "abc"})
    db.expire_all()
    link = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first()
    st, _ = get(opn, f"/p/{link.token}")
    check("有效期填非数字时不崩且链接仍可用", st == 200, f"实际 {st}")
    check("有效期填非数字时回落到 180 天（而不是 0 或永久）",
          179 <= (link.expires_at - link.created_at).days <= 181,
          str((link.expires_at - link.created_at).days))

    # 表单语义：勾选框不提交 = 关闭。这是 HTML 原生行为，
    # 但对「开关型设置」是个坑，所以显式断言一次——
    # 目的是让后来人知道这是**有意**的，不是漏传参数。
    post(opa, f"/projects/{pid}/portal", {"enabled": "on", "expire_days": "30"})
    db.expire_all()
    link = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first()
    check("勾选框未提交时开关被关闭（HTML 表单语义，非 bug）",
          link.show_payment is False)

    # 换 token：旧链接立刻死。先把收款开关打开，才能验证「reset 保留设置」
    post(opa, f"/projects/{pid}/portal",
         {"enabled": "on", "show_payment": "on", "expire_days": "30"})
    db.expire_all()
    link = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first()
    old = link.token
    post(opa, f"/projects/{pid}/portal/reset", {})
    db.expire_all()
    link = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first()
    check("reset 换了新 token", link.token != old)
    st, _ = get(opn, f"/p/{old}")
    check("reset 后旧链接立即失效", st == 404, f"实际 {st}")
    st, _ = get(opn, f"/p/{link.token}")
    check("reset 后新链接可用", st == 200, f"实际 {st}")
    check("reset 保留 enabled（换链接不等于停用）", link.enabled is True)
    check("reset 保留 show_payment（换链接不必重设）", link.show_payment is True)

    # GET 管理页不能改设置（历史上最容易出的那种静默 bug）
    get(opa, f"/projects/{pid}/portal")
    get(opa, f"/projects/{pid}/portal")
    db.expire_all()
    link2 = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first()
    check("反复 GET 管理页不改变任何设置",
          (link2.enabled, link2.show_payment, link2.show_photos, link2.token)
          == (link.enabled, link.show_payment, link.show_photos, link.token),
          f"{(link2.enabled, link2.show_payment, link2.show_photos)}")

    # 停用后管理页不再显示可打开的链接
    post(opa, f"/projects/{pid}/portal", {})
    db.expire_all()
    link = db.query(ProjectPortalLink).filter(
        ProjectPortalLink.project_id == pid).first()
    check("停用后 enabled=False", link.enabled is False)
    check("停用后清空 expires_at", link.expires_at is None)
    st, _ = get(opn, f"/p/{link.token}")
    check("停用后链接失效", st == 404, f"实际 {st}")

    # ================= 11. 一项目一链接 =================
    dup = ProjectPortalLink(project_id=pid, token="pmt-dup")
    db.add(dup)
    try:
        db.commit()
        db.rollback()
        check("同一项目不能有两个链接行", False, "UNIQUE(project_id) 没生效")
    except Exception:
        db.rollback()
        check("同一项目不能有两个链接行", True)

    # ================= 12. 免登录放行 =================
    check("/p/ 在 PUBLIC_PREFIXES 里（否则业主请求被守卫判 403）",
          any(p.startswith("/p/") for p in _public_prefixes()))

    # ================= 13. 占位符地址必须被识别 =================
    # 这不是洁癖：部署时照着文档把示例地址原样抄进 .env 是**高频操作**，
    # 而填了「非空但无效」的值比留空破坏性大得多——
    # 留空会亮出 localhost 警告，占位符却格式完全正常、警告不触发，
    # 于是没人发现，直到业主打不开链接。
    from app.routers.portal import looks_like_placeholder
    check("识别：文档示例原样（你抄了这个）",
          looks_like_placeholder("http://你的对外地址:8018"))
    check("识别：<占位> 尖括号",
          looks_like_placeholder("http://<host>:8018"))
    check("识别：xxx / example.com 之类",
          looks_like_placeholder("http://xxx.example.com"))
    check("识别：空串", looks_like_placeholder(""))
    check("真实地址不被误判",
          not looks_like_placeholder("http://192.168.1.100:8018"))
    check("真实域名不被误判",
          not looks_like_placeholder("https://pm.zhixing.cn"))
    # 子串匹配的陷阱：标记词必须是「不可能出现在真域名里」的。
    # 下面两条锁住两个曾被误伤的案例（todo / example 单独作标记）。
    check("含 todo 的真实地址不被误判",
          not looks_like_placeholder("https://todos.pmdemo.cn"))
    check("含 example 单词但不属保留域名的地址不被误判",
          not looks_like_placeholder("https://pm.example.cn"))
    check("保留域名仍被识别",
          looks_like_placeholder("https://pm.example.com"))

    _f["proj_ids"] = [pid]
    _f["cust_id"] = cust.id
    _f["link_project_ids"] = [pid]
    db.close()


def _public_prefixes():
    from app.permissions import PUBLIC_PREFIXES
    return PUBLIC_PREFIXES


def _headers_of(path):
    """取响应头字符串。找不到路由时返回空串。"""
    try:
        req = urllib.request.Request(BASE + path)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json_dumps_headers(r.headers)
    except urllib.error.HTTPError as e:
        return json_dumps_headers(e.headers)
    except Exception:
        return ""


def json_dumps_headers(h):
    return "\n".join(f"{k}: {v}" for k, v in h.items())


if __name__ == "__main__":
    print("=== 业主只读链接回归 ===")
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
