"""行级权限在**报表类页面**的落实（2026-08 那批修漏了报表区）。

背景
----
`tests/test_row_scope.py` 已经覆盖了项目列表/详情、首页、待办、导出。
但它当时没有覆盖 `app/routers/reports.py`，于是那里漏了三处：

  1. `_profit_data(db)` 没有 user 参数，`db.query(Project)` 全量
     → `/reports/profit` 与 `/reports/profit/export` 泄露全公司合同额与毛利。
     `LEGACY_ROLE_MODULES` 里工程部/财务部/采购部/行政部**默认都有 reports**，
     所以默认配置下就在泄露，不是理论问题。
  2. `/reports/ledger` 的项目切换下拉列全部项目名
     → 范围收紧的用户「看不见数据但看得见有哪些项目」。
  3. 同一页 `?project_id=` 没有二次校验
     → 手输 id 直接看到别人的完整资金台账。这条比 1 更严重：
     台账里是收款明细与发票，不是汇总数。

为什么这类问题必须单独测
----------------------
页面照常渲染、数字照常好看、GET 巡检全绿、没有任何报错。
唯一的症状是「数字比应该的多」。所以断言必须是**数字层面的**：
造一个金额唯一的夹具项目，然后检查那个数字有没有出现在不该出现的地方。

⚠ 运行需要本地 8018 在跑；服务没起以 exit 2 退出。
"""
from __future__ import annotations

import atexit
import os
import re
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8018"

P, F = [], []

# 夹具金额。刻意选一个在真实数据里不可能出现的值：
# 777,777 与 888,888 一眼好认，且不可能与任何既有金额撞上。
LEAK_AMT = 777777.0
LEAK_STR = "777777"          # money 过滤器渲染后不含千分位时的形态
LEAK_STR_C = "777,777"      # 带千分位的形态


def _safe(s) -> str:
    """把字符串压成当前控制台能编码的形式。

    ⚠ Windows 控制台默认 GBK，编码不了 `¥`(U+00A5)、`→`、部分生僻字。
      直接 print 会在**打印失败信息**时抛 UnicodeEncodeError，
      而失败信息恰恰是断言失败时才需要的 —— 于是「测试抓到了 bug」
      这件事本身被吞掉，输出停在半个 [FAIL] 上，人还以为它只是没跑完。
      修法：先试 encode，失败就把不能编码的字符替换成 '?'。
    """
    s = str(s)
    try:
        s.encode(sys.stdout.encoding or "utf-8")
        return s
    except (UnicodeEncodeError, LookupError):
        return s.encode(sys.stdout.encoding or "utf-8",
                        "replace").decode(sys.stdout.encoding or "utf-8",
                                          "replace")


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(_safe(("  [PASS] " if cond else "  [FAIL] ") + name
                + (("  -> " + _safe(extra)) if extra and not cond else "")))
    return cond


def section(t):
    print(_safe("\n" + t))


# ---------------------------------------------------------------- 连通性
try:
    import urllib.request
    urllib.request.urlopen(BASE + "/login", timeout=5)
except Exception as e:      # noqa: BLE001
    print("本地 8018 没在跑：%s" % e)
    sys.exit(2)

from playwright.sync_api import sync_playwright  # noqa: E402

# ---------------------------------------------------------------- 夹具
from app.auth import hash_password, verify_password  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (Contract, CostItem, Customer, Payment,  # noqa: E402
                        Project, User)

MARK = "__rpt_scope__"
DB = SessionLocal()


_own_backup = None      # (project_id, 原 owner_user_id)，用于还原


def cleanup():
    """删干净夹具，并**报出残留数**。

    清理失败是完全静默的：不 print 计数就永远看不见。
    而留下的 777777 夹具会被本文件自己的断言当成「泄露」，
    于是下一次运行必然假红 —— 症状离根因很远。

    ⚠ 还要还原借用的真实项目的 owner_user_id：测试把它指向了
      一个即将被删掉的用户，留着就是一个指向不存在行的外键。
      不还原的话，之后任何按 owner_user_id 判「我负责的项目」的
      页面都会莫名少一个项目，而原因在几天前的测试里。
    """
    try:
        DB.rollback()
        if _own_backup is not None:
            DB.query(Project).filter(
                Project.id == _own_backup[0]).update(
                    {"owner_user_id": _own_backup[1]},
                    synchronize_session=False)
            DB.commit()
        q = DB.query(User).filter(User.username.like(MARK + "%"))
        n_u = len(q.all())
        for u in q.all():
            DB.delete(u)
        qp = DB.query(Project).filter(Project.name.like(MARK + "%"))
        n_p = len(qp.all())
        for p in qp.all():
            DB.query(Payment).filter(Payment.project_id == p.id).delete()
            DB.query(CostItem).filter(CostItem.project_id == p.id).delete()
            DB.query(Contract).filter(Contract.project_id == p.id).delete()
            DB.delete(p)
        DB.commit()
        left = (DB.query(User).filter(User.username.like(MARK + "%")).count()
                + DB.query(Project).filter(Project.name.like(MARK + "%")).count())
        print("[cleanup] 用户 %d / 项目 %d 已删除%s"
              % (n_u, n_p, ("，⚠ 残留 %d 行" % left) if left else ""))
    except Exception as e:      # noqa: BLE001
        print("[cleanup] 失败：%s" % e)
        DB.rollback()


atexit.register(cleanup)
cleanup()      # 先清历次崩溃遗留

U_PWD = "sc0pe-test-pwd"

_cust = DB.query(Customer).order_by(Customer.id).first()
assert _cust, "库里没有客户，无法建夹具"

# 夹具项目：金额唯一，用于检测泄露
_leak = Project(customer_id=_cust.id, name=MARK + "别人的项目",
                status="施工中", designer="某某设计")
DB.add(_leak)
DB.flush()
DB.add(Contract(project_id=_leak.id, name=MARK + "合同",
                amount=LEAK_AMT, sign_date=date.today()))
# 一条已收款，让「已收/应收/台账」里也带上这个金额的痕迹。
# ⚠ 字段名逐个对着 models.py 核过，别照其它模型猜：
#   Payment 用 `name`（节点名），CostItem 用 `item_name`（不是 name），
#   Payment 的作废列是 `void_status`、CostItem 的是 `status`（两个模型不同！）。
DB.add(Payment(project_id=_leak.id, name=MARK + "首付款",
               plan_amount=LEAK_AMT, paid_amount=LEAK_AMT))
DB.add(CostItem(project_id=_leak.id, item_name=MARK + "成本", amount=LEAK_AMT))
DB.commit()

# 自己的项目：让范围收紧的用户能看到**一个**项目。
#
# ⚠ 必须写 owner_user_id，不能写 supervisor/designer/foreman。
#   `data_scope="own"` 在 app/row_scope.py 里**只**生成
#   `Project.owner_user_id == uid` 这一个条件，**完全不看**负责人姓名
#   （姓名匹配是 `assigned` 才对）。第一版图省事写了 supervisor，
#   结果范围收紧的用户一个项目都看不到 —— 于是
#   「毛利报表不含夹具数据」这类断言全部因为「页面是空的」而假绿，
#   而真正要抓的泄露反而被空页面掩盖了。
_own = (DB.query(Project)
        .filter(~Project.name.like(MARK + "%")).order_by(Project.id).first())
_usr = User(username=MARK + "scoped", password_hash=hash_password(U_PWD),
            display_name="范围测试员", role="工程部", is_active=True,
            data_scope="own")
# role 必须是**有 reports 权限**的岗位，否则「毛利报表可打开」直接 403，
# 后面所有断言都因为进不去页面而假绿。
# LEGACY_ROLE_MODULES 里「员工」只有 {dashboard, projects}，没有 reports；
# 「工程部」有 reports 且不是 SUPER_ROLE，所以 data_scope 照样生效 ——
# 这正是现实里最可能踩坑的组合：能看报表，但只该看自己的项目。
assert "工程部" not in ("老板", "admin")
DB.add(_usr)
DB.commit()
if _own:
    _own_backup = (_own.id, _own.owner_user_id)
    _own.owner_user_id = _usr.id
    DB.commit()
OWN_PID = _own.id if _own else 0
LEAK_PID = _leak.id

# ---------------------------------------------------------------- 开跑
with sync_playwright() as pw:
    br = pw.chromium.launch()
    pg = br.new_page(viewport={"width": 1600, "height": 1000})

    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    # =============================================== 1. 老板（全量）应当看得到
    section("[1] 范围=all 的用户（admin）应当看得到夹具数据")
    pg.goto(BASE + "/reports/profit")
    pg.wait_for_load_state("networkidle")
    admin_html = re.sub(r"\{#.*?#\}", "", pg.content(), flags=re.S)
    check("admin 在毛利报表里看得到夹具项目名", MARK in admin_html)
    check("admin 看得到夹具金额",
          LEAK_STR in admin_html or LEAK_STR_C in admin_html)

    # =============================================== 2. 收紧范围的用户看不到
    section("[2] 范围收紧的用户不应当看得到")
    pg.goto(BASE + "/logout")
    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", _usr.username)
    pg.fill("input[name=password]", U_PWD)
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    # 先确认登录确实成功（否则下面全是因为「进不去页面」而假绿）
    check("范围收紧用户登录成功",
          "/login" not in pg.url, pg.url)

    # ---- 假绿守卫（这一段是本文件存在的主要理由之一）----
    # 「页面上没有泄露数据」在「页面根本是空的」时同样成立。
    # 第一版把夹具挂到 supervisor 上，而 data_scope=own 只认 owner_user_id，
    # 于是这个用户一个项目都看不到 —— 下面 6 条泄露断言全部因为
    # 「页面是空白」而通过，把真正要抓的 bug 完美掩盖。
    # 所以先证明「他确实能看到自己的项目」，再谈他看不到别人的。
    pg.goto(BASE + "/reports/profit")
    pg.wait_for_load_state("networkidle")
    check("未落到登录页（否则后面全是假绿）", "/login" not in pg.url, pg.url)
    check("范围收紧用户**能看到自己的项目**（否则泄露断言无意义）",
          pg.locator("table tbody tr").count() > 0,
          "表格 0 行 —— 夹具没挂上，后面所有断言都会假绿")

    # ---- 2a. 毛利报表页面 ----
    resp = pg.goto(BASE + "/reports/profit")
    check("毛利报表可打开（非 403）", resp.status == 200, resp.status)
    profit_html = re.sub(r"\{#.*?#\}", "", pg.content(), flags=re.S)
    check("毛利报表不含夹具项目名", MARK not in profit_html,
          [m for m in re.findall(r"[\w]*__rpt_scope__[\w]*", profit_html)])
    # 关键断言：**数字**层面。汇总行也会泄露（合计含全公司），
    # 所以不能只看「有没有 777777」，得看它在不在页面的金额位上。
    body_txt = pg.inner_text("body")
    check("毛利报表**任何位置**都不含夹具金额（含合计行）",
          LEAK_STR not in body_txt.replace(" ", "")
          and LEAK_STR_C not in body_txt,
          [ln for ln in body_txt.splitlines()
           if LEAK_STR in ln.replace(" ", "") or LEAK_STR_C in ln][:3])

    # ---- 2b. 毛利报表导出（导出文件会离开系统，是最严重的一处）----
    # ⚠ 不用 `pg.goto()` 打开导出地址：那是 xlsx 响应，浏览器会当成下载，
    #   goto 直接抛 net::ERR_ABORTED。第一版就是这么写的，整个测试崩掉，
    #   而且崩在下载上——看起来像导出坏了，其���导出功能完全正常。
    #   改用 page.request（与页面共用 cookie jar），拿到二进制自己落盘。
    resp = pg.request.get(BASE + "/reports/profit/export")
    check("毛利报表导出返回 200", resp.status == 200, resp.status)
    tmp_xlsx = os.path.join(ROOT, "tests", "shots", "_scope_profit_export.xlsx")
    os.makedirs(os.path.dirname(tmp_xlsx), exist_ok=True)
    with open(tmp_xlsx, "wb") as f:
        f.write(resp.body())
    try:
        from openpyxl import load_workbook
        wb = load_workbook(tmp_xlsx, data_only=True)
        found = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for cell in row:
                    if isinstance(cell, (int, float)) and abs(cell - LEAK_AMT) < 0.01:
                        found.append((ws.title, "金额", cell))
                    elif isinstance(cell, str) and MARK in cell:
                        found.append((ws.title, "项目名", cell))
        check("毛利报表**导出**不含夹具项目与金额", not found, found[:4])
    except ImportError:
        raw = resp.body()
        check("毛利报表导出不含夹具金额（文本兜底，未装 openpyxl）",
              LEAK_STR.encode() not in raw)
    finally:
        # 顺手删掉：里面是真实金额的快照，不该留在工作区（会同步到 NAS）
        if os.path.exists(tmp_xlsx):
            os.remove(tmp_xlsx)

    # ---- 2c. 资金台账的项目下拉 ----
    pg.goto(BASE + "/reports/ledger")
    pg.wait_for_load_state("networkidle")
    ledger_html = re.sub(r"\{#.*?#\}", "", pg.content(), flags=re.S)
    check("台账页不含夹具项目名（下拉已过滤）", MARK not in ledger_html,
          [m for m in re.findall(r"[\w]*__rpt_scope__[\w]*", ledger_html)])

    # ---- 2d. 台账页手输 project_id（二次校验）----
    resp = pg.goto(BASE + "/reports/ledger?project_id=%d" % LEAK_PID)
    # 必须 404 而不是 403：403 会泄露「这个项目存在」
    check("手输别人的 project_id 返回 404（不是 403）",
          resp.status == 404, resp.status)
    led_txt = pg.inner_text("body")
    check("手输 id 的响应体不含夹具金额",
          LEAK_STR not in led_txt.replace(" ", "")
          and LEAK_STR_C not in led_txt)

    # ---- 2e. 自己的项目仍然打得开（不能修成「谁都看不到」）----
    if _own:
        resp = pg.goto(BASE + "/reports/ledger?project_id=%d" % _own.id)
        check("自己的项目台账仍然可以打开（没修成全禁）",
              resp.status == 200, resp.status)

    # =============================================== 3. 口径没被改坏
    section("[3] 加过滤之后，口径与排版没坏")
    pg.goto(BASE + "/reports/profit")
    pg.wait_for_load_state("networkidle")
    t = pg.inner_text("body")
    check("毛利报表有合计行", "合计" in t)
    check("表格没有空壳（行数 > 0）",
          pg.locator("table tbody tr").count() > 0)
    # 三个成本口径的表头还在
    for col in ("已下单", "已签约", "未下单"):
        check("成本口径列「%s」还在" % col, col in t)

    pg.goto(BASE + "/reports/ledger")
    pg.wait_for_load_state("networkidle")
    t2 = pg.inner_text("body")
    check("台账页正常渲染（正文 > 200 字符）", len(t2) > 200, len(t2))
    check("台账页未落到登录页", "/login" not in pg.url, pg.url)

    pg.close()
    br.close()

cleanup()

print("\n%d 项通过，%d 项失败" % (len(P), len(F)))
if F:
    print("失败：")
    for n in F:
        print("  - " + n)
sys.exit(1 if F else 0)