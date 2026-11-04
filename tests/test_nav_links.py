"""侧栏导航不得有死链。

起因：侧栏「项目台账」点进去是 404。permissions.py 里 reports 模块的
prefixes=('/reports',)，而侧栏拿 prefixes[0] 当跳转地址——/reports 本身
没有任何路由，页面都在 /reports/ledger 与 /reports/profit 下。

修法是给 ModuleDef 加 nav_url 单独给导航用，prefixes 保持粗粒度继续
服务权限匹配（改成 /reports/ledger 会让 /reports/profit 匹配不上模块，
权限直接失效）。

这里要防的是**同类问题再次发生**，所以检查的是「每个侧栏项点进去
必须 200」，而不是「reports 那一项是不是 200」。
"""
from __future__ import annotations

import io
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.permissions import MODULES, nav_modules  # noqa: E402

BASE = "http://127.0.0.1:8018"
P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


print("=" * 78)
print("[1] nav_url 字段存在，且 reports 指向真实页面")
print("=" * 78)
m = MODULES["reports"]
check("reports 有 nav_url", bool(m.nav_url), m.nav_url)
check("reports 的 prefixes 仍是 /reports（权限匹配不能改细）",
      m.prefixes == ("/reports",), m.prefixes)
check("nav_url 落在 prefixes 之内（权限仍能匹配到）",
      any(m.nav_url.startswith(p) for p in m.prefixes), (m.nav_url, m.prefixes))

print()
print("=" * 78)
print("[2] 侧栏模板优先用 nav_url")
print("=" * 78)
sb = io.open(os.path.join(ROOT, "app", "templates", "_sidebar.html"),
             encoding="utf-8", errors="replace").read()
check("模板引用了 m.nav_url", "m.nav_url" in sb, sb[:200])
check("保留了 prefixes[0] 兜底", "prefixes[0]" in sb)

print()
print("=" * 78)
print("[3] 每个侧栏项点进去必须 200（逐个真点）")
print("=" * 78)
cj = CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
data = urllib.parse.urlencode({"username": "admin", "password": "admin123"}).encode()
op.open(urllib.request.Request(BASE + "/login", data=data, method="POST"),
        timeout=20).read()

dead = []
for m in nav_modules():
    url = m.nav_url or (m.prefixes[0] if m.prefixes[0] != "/dashboard" else "/")
    try:
        r = op.open(BASE + url, timeout=25)
        r.read()
        st = r.status
    except urllib.error.HTTPError as e:
        e.read()
        st = e.code
    except Exception as e:
        st = "ERR:%s" % type(e).__name__
    flag = "" if st == 200 else "   <== 死链"
    print("  %-18s %-26s %s%s" % (m.key, url, st, flag))
    if st != 200:
        dead.append((m.key, url, st))

check("无死链（%d 个导航项全部 200）" % len(nav_modules()),
      not dead, dead)

print()
print("=" * 78)
print("[4] 页面内导航也扫一遍（正文里的链接同样会 404）")
print("=" * 78)
# 采集所有模板里指向本系统的 href，去重后逐个访问。
# 只查 GET 可见页面，跳过删除/编辑等带 {id} 的动态路由（要具体 id 才能测）。
import glob
import re

TPL = os.path.join(ROOT, "app", "templates")
urls = set()
for f in glob.glob(os.path.join(TPL, "**", "*.html"), recursive=True):
    s = io.open(f, encoding="utf-8", errors="replace").read()
    for mm in re.finditer(r'href="(/[^"{}]*)"', s):
        u = mm.group(1)
        if u.startswith("//") or u.startswith("/static") or u.startswith("/uploads"):
            continue
        if "logout" in u:
            continue
        urls.add(u)

bad2 = []
for u in sorted(urls):
    try:
        r = op.open(BASE + u, timeout=25)
        r.read()
        st = r.status
    except urllib.error.HTTPError as e:
        e.read()
        st = e.code
    except Exception as e:
        st = "ERR:%s" % type(e).__name__
    if st != 200:
        bad2.append((u, st))
print("  扫描 %d 个静态链接，异常 %d 个" % (len(urls), len(bad2)))
for u, st in bad2:
    print("    ! %-46s %s" % (u[:44], st))
# POST-only 路由用 GET 访问会 405，那是设计如此，不是死链
real_dead = [(u, st) for u, st in bad2 if st != 405]
check("无真实死链（405 是 POST-only 路由，设计如此）", not real_dead, real_dead)

print()
print("=" * 78)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
print("=" * 78)
sys.exit(1 if F else 0)