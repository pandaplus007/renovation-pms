"""经营大屏 /dashboard/screen 回归。

需要本地 8018 在跑（要真实渲染才能验证 CSS 与对比度）。
服务没起以 exit 2 退出，与 test_gantt_board.py 同一约定。

这个测试盯三类问题，顺序即严重度：

  1. **行级权限泄露**。大屏是新页面，最容易犯的错是忘了套
     visible_projects——症状是收紧范围的员工在投屏上看到全公司合同额。
     这类错误不会让任何页面报错，所以只能靠「造两个范围差异的用户，
     断言大屏数字不同」来证明过滤真的生效。
  2. **状态色写法契约**。`state-badge state-{{ x | state_badge }}`
     会渲染成 `state-state-badge`，症状是徽章拿不到背景色。
     与 tests/test_state_contract.py 的 [G] 节同一类问题，这里只看本页面。
  3. **口径漂移**。大屏的数字必须与首页 / 毛利报表一致，
     否则老板对着大屏和对着报表会看到两个公司的数。
"""
from __future__ import annotations

import atexit
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8018"
SCREEN = "/dashboard/screen"

P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))
    return cond


def section(t):
    print("\n" + t)


# ---------------------------------------------------------------- 连通性
try:
    import urllib.request
    urllib.request.urlopen(BASE + "/login", timeout=5)
except Exception as e:      # noqa: BLE001
    print("本地 8018 没在跑：%s" % e)
    print("先 `python run.py`，再跑本测试。")
    sys.exit(2)

from playwright.sync_api import sync_playwright  # noqa: E402

# ---------------------------------------------------------------- 夹具
# 造一个「只看自己项目」的用户 + 一个不属于他的项目。
# 断言大屏上的合同额合计**不含**别人的项目，而不是断言页面上没有那句话——
# 数字对不上才是泄露，页面上没出现某些字不代表数字被过滤了。
from app.database import SessionLocal  # noqa: E402
from app.models import Contract, Project, User  # noqa: E402
from app.row_scope import project_scope_filter  # noqa: E402

MARK = "__scr_test__"
DB = SessionLocal()
_u = _p = _c = None


def cleanup():
    """删干净本轮夹具。

    ⚠ RoleModulePerm 是**岗位**→模块 的表（列是 role/module/flags），
      没有 user_id 列。用户权限是 users.role → roles → role_module_perms，
      所以按用户建岗位权限行这条路根本不存在，删它属于凭想象写的代码。
      第一版写了这么一行，清理**每次都失败**——而清理失败是完全静默的，
      唯一线索就是这行 print，于是夹具（一个 999999 的项目）永久留在库里，
      会污染之后所有依赖项目数量的测试。
    """
    try:
        DB.rollback()
        # 开跑前先清掉历次崩溃的遗留：username 是 UNIQUE，残留会让下一轮
        # `DB.add(User(...))` 撞 IntegrityError，报错指向唯一约束，
        # 跟真正的问题（残留）毫无关系。
        q = DB.query(User).filter(User.username.like(MARK + "%"))
        n_u = len(q.all())
        for u in q.all():
            DB.delete(u)
        qp = DB.query(Project).filter(Project.name.like(MARK + "%"))
        n_p = len(qp.all())
        for p in qp.all():
            DB.query(Contract).filter(Contract.project_id == p.id).delete()
            DB.delete(p)
        DB.commit()
        # 报出删了几条：清理失败是完全静默的，看不见就会留下脏数据。
        print("[cleanup] 用户 %d / 项目 %d 已删除" % (n_u, n_p))
        left = (DB.query(User).filter(User.username.like(MARK + "%")).count()
                + DB.query(Project).filter(Project.name.like(MARK + "%")).count())
        if left:
            print("[cleanup] ⚠ 仍有 %d 行残留" % left)
    except Exception as e:      # noqa: BLE001
        print("[cleanup] 失败：%s" % e)
        DB.rollback()


atexit.register(cleanup)
cleanup()          # 先清历次崩溃遗留，再造新夹具

with sync_playwright() as pw:
    br = pw.chromium.launch()

    def login(page, user="admin", pwd="admin123"):
        page.goto(BASE + "/login")
        page.fill("input[name=username]", user)
        page.fill("input[name=password]", pwd)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")

    # =========================================================== 1. 页面可达
    section("[1] 页面可达与渲染")
    pg = br.new_page(viewport={"width": 1920, "height": 1080})
    login(pg)
    pg.goto(BASE + SCREEN)
    pg.wait_for_load_state("networkidle")
    html = pg.content()
    # 剥掉 Jinja 注释再断言：注释里常包含要查的词，否则「页面不出现某词」
    # 这类断言会永远假绿。
    html_nc = re.sub(r"\{#.*?#\}", "", html, flags=re.S)

    check("HTTP 200 且无 500 痕迹", "装修项目管理" not in html_nc
          and "Internal Server Error" not in html_nc)
    check("有主数字带（.scr-hero）", html_nc.count("scr-hero-cell") >= 5,
          html_nc.count("scr-hero-cell"))
    check("有待办区", "scr-lamp" in html_nc)
    check("有大屏样式表", "screen.css" in html_nc)

    # =========================================================== 2. 契约：不被继承的东西
    section("[2] 大屏不该带工作区外壳")
    # 侧栏 24 个导航项投屏时既占地方又暴露系统结构。
    # 数字角标、外层 .main/.topbar 同理——它们属于 ERP 工作区，不属于大屏。
    check("没有侧栏", 'class="sidebar' not in html_nc)
    check("没有 topbar", 'class="topbar' not in html_nc)
    check("没有轮询铃铛（/api/alerts）", "/api/alerts" not in html_nc)

    # =========================================================== 2b. 返回主界面
    section("[2b] 极简外壳必须自带回路")
    # 大屏不继承 base.html ⇒ 没有侧栏、没有顶栏，第一版也就没有任何返回入口，
    # 出大屏只能按浏览器后退键——而投屏设备多半是常开的 kiosk 浏览器，
    # 压根没有后退键。这就是「有去无回」。盯的是入口存在、指向对、真能点。
    back = pg.locator("a.scr-back")
    n_back = back.count()
    check("有返回主界面的入口（%d 个）" % n_back, n_back == 1, n_back)
    href = back.get_attribute("href") if n_back else ""
    check("返回入口指向首页 /（不是 #、不是 javascript:）", href == "/", href)
    if n_back:
        back.click()
        pg.wait_for_load_state("networkidle")
        check("点返回真的落到首页", pg.url.rstrip("/") == BASE, pg.url)
        # 落点是**工作区**（有侧栏），而不是又一个大屏
        check("回到的是带侧栏的工作区首页",
              'class="sidebar' in pg.content())
    pg.goto(BASE + SCREEN)
    pg.wait_for_load_state("networkidle")

    # =========================================================== 3. 状态色契约
    section("[3] 状态色写法契约")
    # 过滤器返回的已经是 `state-badge state-xxx` 完整类串。
    # 模板里再写一遍 `state-badge state-` 就会渲染成 state-state-badge，
    # 背景色拿不到 —— 症状是徽章变透明，不是报错。
    bad = re.findall(r'state-state-\w+', html_nc)
    check("没有 state-state-* 这类拼错的类", not bad, bad[:5])
    # 每个状态徽章都必须真的带上了背景类
    badges = re.findall(r'<span class="scr-dist-name ([^"]*)"', html_nc)
    nobg = [b for b in badges if "state-badge" not in b]
    check("状态徽章都带 .state-badge（%d 个）" % len(badges), not nobg, nobg[:3])
    # ⚠ 不能用 `state-(\w+)` 取档位：类串是 `scr-dist-name state-badge state-idle`，
    #   第一个 `state-` 匹配到的是 `state-badge`，取出来的集合是 {'badge'}，
    #   于是断言「档位不在语义档内」恒真失败——这条断言自己也踩了
    #   AGENTS.md 里「同一个词在两处含义不同」那一类。
    #   正确做法：取**最后一个** state-* 类。
    tones = set()
    for b in badges:
        found = re.findall(r"\bstate-(\w+)", b)
        tones.update(found)
    tones.discard("badge")          # .state-badge 是背景类前缀，不是档位
    check("状态→颜色的档位都在 4+1 语义档内", tones <= {
        "done", "active", "watch", "idle", "alert"}, tones)

    # =========================================================== 4. 两个进度分清
    section("[4] 两个进度必须分清")
    # AGENTS.md：装修行业施工与回款经常反向，合成一个「进度」会读错。
    check("同时出现「施工」", "施工" in html_nc)
    check("同时出现「回款」", "回款" in html_nc)
    check("双进度区存在", html_nc.count("scr-dual-li") >= 1
          or "没有可对比进度的项目" in html_nc)

    # =========================================================== 5. 大屏专属字号刻度
    section("[5] 大屏字号刻度独立于全站六档")
    # 全站六档是 12/14/16/20/24/28（test_type_scale.py）。大屏必须更大，
    # 所以不能复用 --fs-*，而 test_type_scale.py 的 PAGES 里也**不能**加本页面，
    # 否则那条「字号只有 6 档」的断言会被大屏的数字打破。
    sizes = pg.evaluate("""
      () => [...document.querySelectorAll('.scr *')]
        .filter(e => e.children.length === 0 && e.textContent.trim())
        .map(e => Math.round(parseFloat(getComputedStyle(e).fontSize) * 100) / 100)
    """)
    sizes = sorted(set(sizes))
    scr_only = [s for s in sizes if s > 28]
    check("存在大于 28px 的数字（大屏的意义就在这）", bool(scr_only), sizes)
    check("字号种类收敛（≤ 8 档，实际 %d 档）" % len(sizes), len(sizes) <= 8,
          sizes)

    # =========================================================== 5b. 列表行装得下文字
    section("[5b] 待办/双进度的行不许被压扁")
    # 用户报的现象是「文字全部挤到一起」。第一版这里断言的是「相邻行不重叠」，
    # 实测**抓不到**——真实现象不是行与行叠在一起，而是：
    #   行高被压得比一行文字还矮（实测 1920 下 16px、1280 下 9px，
    #   而 line-height 是 18px），overflow:hidden 于是把每行上下各裁一截，
    #   视觉上就成了「挤成一团」。
    # 所以正确的指标是：**行高必须装得下它自己的文字**。
    # 固定轨道（clamp 出的确定值）保证这一点；auto / minmax(0,1fr) 都不保证。
    fit = pg.evaluate("""
      () => {
        const bad = [];
        for (const sel of ['.scr-todo', '.scr-dual']) {
          const list = document.querySelector(sel);
          if (!list) continue;
          for (const el of list.children) {
            if (el.offsetParent === null) continue;          // 被 JS 隐藏的
            const h = el.getBoundingClientRect().height;
            const cs = getComputedStyle(el);
            // 需要的最小高度 = 行高 + 上下内边距
            const need = parseFloat(cs.lineHeight)
                       + parseFloat(cs.paddingTop)
                       + parseFloat(cs.paddingBottom);
            if (h + 0.5 < need) {
              bad.push({ sel: sel, h: Math.round(h*10)/10,
                         need: Math.round(need*10)/10,
                         t: el.textContent.trim().slice(0, 12) });
            }
          }
        }
        return bad;
      }
    """)
    check("每行都装得下自己那行文字（不被压扁）", not fit, fit[:4])

    rows = pg.evaluate("""
      () => {
        const bad = [];
        for (const sel of ['.scr-todo', '.scr-dual']) {
          const list = document.querySelector(sel);
          if (!list) continue;
          const vis = [...list.children]
            .filter(e => e.offsetParent !== null)
            .map(e => e.getBoundingClientRect())
            .sort((a, b) => a.top - b.top);
          for (let i = 1; i < vis.length; i++) {
            if (vis[i].top < vis[i - 1].bottom - 0.5) {
              bad.push({ sel: sel, i: i,
                         overlap: Math.round((vis[i-1].bottom - vis[i].top) * 10) / 10 });
            }
          }
        }
        return bad;
      }
    """)
    check("相邻行不重叠", not rows, rows[:4])

    h_rows = pg.evaluate("""
      () => [...document.querySelectorAll('.scr-todo > li:not([hidden])')]
        .map(e => Math.round(e.getBoundingClientRect().height))
    """)
    check("可见待办行高一致（%s）" % h_rows[:6],
          len(set(h_rows)) <= 1, h_rows)

    # ⚠ 这一节必须在**多档视口**下各跑一遍，不能只看 1920。
    #   第一版只在 1920x1080 断言，而那档的行高恰好等于文字所需高度
    #   （16.3 == 16.3）——通过了；到了 1366 / 1280 行高被压到
    #   10.7 / 9.3px，文字全被裁掉，而测试完全没看见。
    #   「一档通过」在这里等于「没测」：压扁的程度随视口高度线性变化，
    #   只有最矮的那档才暴露。
    for _w, _h in ((1920, 1080), (1366, 768), (1280, 720)):
        pg.set_viewport_size({"width": _w, "height": _h})
        pg.goto(BASE + SCREEN)
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(250)
        fit = pg.evaluate("""
          () => {
            const bad = [];
            for (const sel of ['.scr-todo', '.scr-dual']) {
              const list = document.querySelector(sel);
              if (!list) continue;
              for (const el of list.children) {
                if (el.offsetParent === null) continue;
                const cs = getComputedStyle(el);
                const need = parseFloat(cs.lineHeight)
                           + parseFloat(cs.paddingTop)
                           + parseFloat(cs.paddingBottom);
                const h = el.getBoundingClientRect().height;
                if (h + 0.5 < need) {
                  bad.push({ sel: sel, h: Math.round(h*10)/10,
                             need: Math.round(need*10)/10 });
                }
              }
            }
            return bad;
          }
        """)
        check("%dx%d 每行都装得下文字" % (_w, _h), not fit, fit[:3])
    pg.set_viewport_size({"width": 1920, "height": 1080})

    # =========================================================== 5c. DOM 结构没被浏览器「自动纠错」
    section("[5c] 卡片是 .scr-grid 的直接子元素")
    # ⚠ 这条是本文件存在的一个教训留下的。
    #   模板里 `<div class="scr-dist">` 漏了 `</div>`，后面 4 张卡片
    #   全成了它的子节点。**浏览器遇到 <section> 会自动闭合上一个块**，
    #   所以页面照样渲染、服务端测试全绿、截图也能看——只是内容全挤在
    #   一起。这是 AGENTS.md「模板区块插错位置不会报错」那一类。
    #   肉眼要等到发现「怎么所有卡片都叠在左上角」才反应过来。
    #   DOM 断言能直接抓到：卡片必须是 .scr-grid 的**直接子元素**。
    struct = pg.evaluate("""
      () => {
        const g = document.querySelector('.scr-grid');
        const cards = [...document.querySelectorAll('.scr-card')];
        return {
          gridChildren: g.children.length,
          cards: cards.length,
          // 每张卡片的父元素都必须带 scr-grid
          badParent: cards
            .filter(c => !c.parentElement.classList.contains('scr-grid'))
            .map(c => c.parentElement.className.slice(0, 24)),
        };
      }
    """)
    check(".scr-grid 有 %d 个直接子元素" % struct["gridChildren"],
          struct["gridChildren"] == struct["cards"],
          (struct["gridChildren"], struct["cards"]))
    check("每张卡片的父元素都是 .scr-grid（没有漏闭合的容器）",
          not struct["badParent"], struct["badParent"])

    # 卡片之间不得互相压住（上下左右都不许）
    boxes = pg.evaluate("""
      () => [...document.querySelectorAll('.scr-card')].map(c => {
        const r = c.getBoundingClientRect();
        return { x: r.x, y: r.y, r: r.right, b: r.bottom,
                 w: r.width, h: r.height };
      })
    """)
    hit = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if (a["x"] < b["r"] - 1 and b["x"] < a["r"] - 1
                    and a["y"] < b["b"] - 1 and b["y"] < a["b"] - 1):
                hit.append((i, j))
    check("卡片之间没有重叠（%d 张）" % len(boxes), not hit, hit[:4])

    # =========================================================== 6. 深色对比度
    section("[6] 大屏逐元素对比度（深色主题）")
    # 大屏固定深色底，且字号远大于工作区，所以这套对比度**独立于**
    # test_dark_contrast.py 的 2029 个样本 —— 那份只扫列出的那几个页面。
    # 这里只查大屏自己，且按投屏距离把阈值降到 3:1（大字号 WCAG AA 本就是 3:1）。
    samples = pg.evaluate("""
      () => {
        const lum = (c) => {
          const [r,g,b] = c.match(/\\d+(\\.\\d+)?/g).slice(0,3).map(Number)
            .map(v => { v /= 255; return v <= .03928 ? v/12.92
              : Math.pow((v+.055)/1.055, 2.4); });
          return .2126*r + .7152*g + .0722*b;
        };
        const bgOf = (el) => {
          let n = el;
          while (n && n !== document.documentElement) {
            const bg = getComputedStyle(n).backgroundColor;
            if (bg && !/rgba?\\(0, 0, 0, 0\\)|transparent/.test(bg)) return bg;
            n = n.parentElement;
          }
          return getComputedStyle(document.body).backgroundColor;
        };
        const out = [];
        for (const el of document.querySelectorAll('.scr *')) {
          if (el.children.length) continue;
          const t = el.textContent.trim();
          if (!t) continue;
          const cs = getComputedStyle(el);
          if (cs.visibility === 'hidden' || cs.display === 'none') continue;
          const bg = bgOf(el);
          const l1 = lum(cs.color), l2 = lum(bg);
          const ratio = (Math.max(l1,l2) + .05) / (Math.min(l1,l2) + .05);
          out.push({ ratio: Math.round(ratio*100)/100, cls:
            String(el.className).slice(0,40), t: t.slice(0,14),
            size: Math.round(parseFloat(cs.fontSize)) });
        }
        return out;
      }
    """)
    low = [s for s in samples if s["ratio"] < 3.0]
    check("全部文字对比度 ≥ 3:1（%d 个样本）" % len(samples), not low,
          [(s["ratio"], s["cls"], s["t"]) for s in low[:6]])
    if low:
        print("      最低几个：")
        for s in sorted(samples, key=lambda x: x["ratio"])[:6]:
            print("        %.2f:1  %-24s %s" % (s["ratio"], s["cls"][:24], s["t"]))

    # =========================================================== 7. 行级权限
    section("[7] 行级权限：范围收紧时数字必须变")
    # 关键：断言**数字**不同，不是断言页面上没有某个项目名。
    # 大屏是投屏页，泄露的是金额，不是文字。
    try:
        from datetime import date as _date

        from app.auth import hash_password
        from app.models import Customer

        # projects.customer_id 是 NOT NULL，必须先拿到一个真实客户。
        # 第一版没设，flush 抛 IntegrityError —— 症状指向 customer_id，
        # 与真正的意图（造行级权限夹具）毫无关系。
        cust = DB.query(Customer).order_by(Customer.id).first()
        if not check("库里至少有一个客户（夹具前置条件）", cust is not None):
            raise RuntimeError("没有客户，无法建夹具")

        proj = Project(customer_id=cust.id, name=MARK + "别人的项目",
                       status="施工中")
        DB.add(proj)
        DB.flush()
        DB.add(Contract(project_id=proj.id, name="合同",
                        amount=999999.0, sign_date=_date.today()))
        DB.commit()

        # 借一个真实项目当「自己的」，保证范围差异之外还有可见数据
        own = (DB.query(Project).filter(~Project.name.like(MARK + "%"))
                .order_by(Project.id).first())
        # ⚠ User 的字段是 `role` 与 `display_name`，**不是** `role_name`/`name`。
        #   User 没有 owner_user_id（那是 Project 上的列），也没有 User.name。
        #   第一版照 Role 的写法猜了三个字段，直接 TypeError。
        #   范围收紧靠 data_scope="own"，匹配 Project 的负责人文本。
        _u = User(username=MARK + "scoped", password_hash=hash_password("x" * 12),
                  display_name="范围测试", role="员工", is_active=True,
                  data_scope="own")
        DB.add(_u)
        DB.commit()

        # 「自己的项目」靠负责人文本匹配（row_scope 对 assigned/own 就是
        # `like %name%`）。所以把用户显示名写进一个真实项目的 supervisor。
        # 不依赖 owner_user_id —— 那个列存量全是 NULL，用它会让用例
        # 「因为匹配不到任何项目而看不到夹具项目」变成假绿。
        if own:
            own.supervisor = _u.display_name
            DB.commit()

        # 直接构造 SQL 条件并执行，确认「别人的项目」不在结果里
        _cond = project_scope_filter(_u, Project)
        q = DB.query(Project).filter(_cond)
        got = [p.name for p in q.all()]
        check("范围收紧的用户看不到夹具项目",
              all(MARK not in g for g in got), [g for g in got if MARK in g])

        # 用真实 HTTP 登录这个用户拿大屏
        pg2 = br.new_page(viewport={"width": 1920, "height": 1080})
        pg2.goto(BASE + "/login")
        pg2.fill("input[name=username]", _u.username)
        pg2.fill("input[name=password]", "x" * 12)
        pg2.click("button[type=submit]")
        pg2.wait_for_load_state("networkidle")
        resp = pg2.goto(BASE + SCREEN)
        check("范围收紧用户能打开大屏（继承 dashboard 模块权限）",
              resp.status == 200, resp.status)
        h2 = re.sub(r"\{#.*?#\}", "", pg2.content(), flags=re.S)
        check("大屏上**看不到**夹具项目的名字（泄露检查）",
              MARK not in h2)
        # 金额泄露的直接证据：夹具项目金额 999,999 不应出现在大屏任何数字里
        big = pg2.evaluate("""
          () => [...document.querySelectorAll('.scr-hero-num')]
            .map(e => e.textContent.replace(/[^0-9]/g,''))
        """)
        check("夹具项目的 999,999 没出现在主数字里",
              not any("999999" in b or b == "999999" for b in big), big)
        check("顶栏显示了数据范围（不是 all）",
              "范围" in h2 or "全公司" in h2)
        pg2.close()
    except Exception as e:      # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("行级权限用例可执行", False, e)
    # ⚠ 这里**不要** cleanup()。第 7b 节（项目切换）还要用同一个夹具项目
    #    测「范围收紧的用户下拉里没有它、手输它的 id 得 404」。
    #    上一版在这里就清了，7b 拿不到夹具 → 越权用例抛 NoneType.id，
    #    而报出来的是「'NoneType' object has no attribute 'id'」——
    #    与真正的意图（权限）毫无关系。清理统一放在最后（atexit 已注册）。

    # =========================================================== 7b. 按项目切换
    section("[7b] 按项目切换")
    pg.goto(BASE + SCREEN)
    pg.wait_for_load_state("networkidle")
    n_all = pg.locator("#scrProject option").count()
    check("顶部有项目切换下拉（%d 个选项，含「全部项目」）" % n_all, n_all >= 2)

    opt = pg.evaluate("""() => {
      const s = document.getElementById('scrProject');
      const v = [...s.options].find(o => o.value);
      return v ? v.value : '';
    }""")
    check("下拉里有可选项目 id", bool(opt), opt)

    # 全公司视图：分布与双进度在，单项目视图那套不在
    check("全公司视图显示「项目状态分布」", "scr-dist" in html_nc)
    check("全公司视图显示「施工/回款 差距」", "scr-dual-li" in html_nc)
    check("全公司视图**不**显示阶段进度（无对象）", "scr-phase-li" not in html_nc)

    resp = pg.goto(BASE + SCREEN + "?project_id=" + opt)
    check("带 project_id 能打开（200）", resp.status == 200, resp.status)
    single = re.sub(r"\{#.*?#\}", "", pg.content(), flags=re.S)
    check("单项目视图**不**显示项目状态分布（1 行没意义）",
          "scr-dist" not in single)
    # 有阶段记录时画阶段行；一个阶段都没录的项目画空状态提示。
    # 两种都算对——**不能**只断言 `scr-phase-li`，那会把「该画空状态」
    # 的项目误判成「没画阶段」，而实际上 project_phases 允许为空。
    check("单项目视图换成阶段进度（有记录画行，无记录给空态提示）",
          ("scr-phase-li" in single)
          or ("该项目还没有阶段记录" in single))
    check("单项目视图不再显示双进度（没有对比对象）",
          "scr-dual-li" not in single)
    # 用 DOM 的实际选中值判断，而不是正则去 HTML 里找 `selected`——
    # 模板换行/缩进一变正则就失效，而 select.value 永远是语义。
    sel_val = pg.evaluate(
        "() => { const s = document.getElementById('scrProject');"
        " return s ? s.value : ''; }")
    check("下拉里该项目被标为选中", sel_val == opt, (sel_val, opt))

    # 404：越权 id 与不存在的 id 都不能泄露存在性
    resp = pg.goto(BASE + SCREEN + "?project_id=99999999")
    check("不存在的 project_id 返回 404", resp.status == 404, resp.status)

    # 越权：范围收紧的用户手输别人的 project_id
    try:
        from app.models import Project as _P
        leak_pid = DB.query(_P).filter(_P.name.like(MARK + "%")).first().id

        # ⚠ 必须**真的换成范围收紧用户的会话**再断言。
        #   上一版直接用 admin 的 pg 去查下拉——那是全量视图，
        #   当然能看到夹具项目，于是断言「下拉里没有它」必然失败，
        #   而报出来的 extra 是 ['3','2','1']，看不出是会话用错了。
        #   这类「拿错身份」的失败比断言本身更误导：
        #   它看起来像功能没做，而不是测试没切账号。
        pg.goto(BASE + "/logout")
        pg.goto(BASE + "/login")
        pg.fill("input[name=username]", _u.username)
        pg.fill("input[name=password]", "x" * 12)
        pg.click("button[type=submit]")
        pg.wait_for_load_state("networkidle")
        check("已切到范围收紧用户的会话", "/login" not in pg.url, pg.url)

        pg.goto(BASE + SCREEN)
        pg.wait_for_load_state("networkidle")
        h = re.sub(r"\{#.*?#\}", "", pg.content(), flags=re.S)
        ids = re.findall(r'<option value="(\d+)"', h)
        check("范围收紧用户的下拉里**没有**夹具项目",
              str(leak_pid) not in ids, ids)

        resp = pg.goto(BASE + SCREEN + "?project_id=" + str(leak_pid))
        body = pg.inner_text("body")
        check("范围收紧用户手输夹具 project_id 得 404（不是 200 空白页）",
              resp.status == 404 and "项目不存在" in body,
              (resp.status, body[:60]))
    except Exception as e:      # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("项目切换的越权用例可执行", False, e)
    finally:
        cleanup()

    # =========================================================== 8. 全部 GET 巡检兼容
    section("[8] 路由登记")
    # GET 路由动作在 build_route_index 里默认兜底为 view，所以不需要登记
    # app/_route_actions.json（那条只针对写操作）。这里把结论固化成断言。
    from app.main import app
    from app.permissions import build_route_index, resolve_module
    idx = build_route_index(app.routes)
    mod, act = idx.lookup(SCREEN, "GET")
    check("resolve_module 认得 %s" % SCREEN,
          resolve_module(SCREEN) == "dashboard", resolve_module(SCREEN))
    check("GET 动作兜底为 view（无需登记 _route_actions.json）",
          (mod, act) == ("dashboard", "view"), (mod, act))

    pg.close()
    br.close()

print("\n%d 项通过，%d 项失败" % (len(P), len(F)))
if F:
    print("失败：")
    for n in F:
        print("  - " + n)
sys.exit(1 if F else 0)