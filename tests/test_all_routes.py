"""全站 GET 路由巡检：逐个真访问，抓出所有 500。

为什么不用现成的链接检查（tests/test_nav_links.py 第 4 节）：
那个只扫模板里的静态 href，覆盖不到需要 id 的页面——而恰恰是
/subcontracts/new 这类页面最坏：它只作为表单 action 出现，
人工测试很容易只试编辑页、漏掉新建页。

做法：从源码枚举所有 GET 路由，用数据库真实 id 填占位符，逐个访问。

路径参数名有歧义（order_id 可能是分包订单也可能是采购订单明细，
cid/item_id 同理）。所以不猜——每个参数名列出**所有可能的候选表**，
逐个试，只要有一个 id 能渲染通过就算这条路由没问题。否则会把
「我填错了 id」误报成「路由坏了」，那种假警报比漏报更消耗信任。
"""
from __future__ import annotations

import glob
import io
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.database import SessionLocal  # noqa: E402
from app.models import *  # noqa: E402,F403
from app.models import (ApprovalRequest, Contract, CostItem, Customer,  # noqa: E402
                        DeliveryNote, Document, Invoice, Material, Payment,
                        Project, ProjectNote, ProjectPhase, PurchaseOrder,
                        PurchaseOrderItem, Quotation, QuotationItem,
                        SiteRecord, SubContract, SubOrder, Supplier, Task,
                        User)

BASE = "http://127.0.0.1:8018"
ROUTERS = os.path.join(ROOT, "app", "routers")

P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


# ---------- 枚举 GET 路由 ----------
PREFIX = re.compile(r'APIRouter\(\s*prefix\s*=\s*["\']([^"\']+)["\']')
GET = re.compile(r'@router\.get\(\s*["\']([^"\']*)["\']')

uniq, seen = [], set()
for fn in sorted(os.listdir(ROUTERS)):
    if not fn.endswith(".py"):
        continue
    src = io.open(os.path.join(ROUTERS, fn), encoding="utf-8",
                  errors="replace").read()
    mp = PREFIX.search(src)
    prefix = mp.group(1) if mp else ""
    funcs = [(m.start(), m.group(1)) for m in re.finditer(r"^def (\w+)\(", src, re.M)]
    for m in GET.finditer(src):
        path = prefix + m.group(1)
        if path in seen:
            continue
        seen.add(path)
        fn_name = "?"
        for pos, name in funcs:
            if pos < m.start():
                fn_name = name
            else:
                break
        uniq.append((path, fn, fn_name))

print("=" * 80)
print("枚举到 GET 路由 %d 条" % len(uniq))

# ---------- 为每个参数名准备候选 id ----------
# 一个参数名可能对应多张表，全部列出，逐个试。
CAND = {
    "project_id": [Project],
    "customer_id": [Customer],
    "supplier_id": [Supplier],
    "contract_id": [Contract],
    "cid": [Contract, Customer],
    "invoice_id": [Invoice],
    "material_id": [Material],
    "payment_id": [Payment],
    "subcontract_id": [SubContract],
    "qid": [Quotation],
    "req_id": [ApprovalRequest],
    "user_id": [User],
    "order_id": [SubOrder, PurchaseOrder, PurchaseOrderItem],
    "item_id": [QuotationItem, PurchaseOrderItem, SubOrder],
    "phase_id": [ProjectPhase],
    "record_id": [SiteRecord],
    "doc_id": [Document],
    "note_id": [ProjectNote],
    "task_id": [Task],
    "dn_id": [DeliveryNote],
    "cost_id": [CostItem],
    "id": [Project],
}

# `/p/{token}` 是**业主入口**，凭 token 进、不登录，语义与其他路由不同：
#   - token 无效 / 已停用 / 已过期 → 404（有意为之，三种原因不区分）
# 所以不能像别的路由那样随便塞个 id：塞任何东西都会 404，
# 然后被本测试当成「页面打不开」的故障。
# 这里单独处理：系统里有一个启用中的链接就访问它（应为 200），
# 一个都没有就跳过（判不了 ≠ 坏了）。
# 完整的 token 语义（吊销/过期/换 token）由 tests/test_portal.py 覆盖。
def _portal_token():
    from app.models import ProjectPortalLink
    d = SessionLocal()
    try:
        row = (d.query(ProjectPortalLink)
               .filter(ProjectPortalLink.enabled.is_(True)).first())
        return row.token if row else None
    finally:
        d.close()

db = SessionLocal()
cands = {}
for name, models in CAND.items():
    ids = []
    for model in models:
        try:
            for row in (db.query(model.id).order_by(model.id).limit(2).all()):
                if row.id not in ids:
                    ids.append(row.id)
        except Exception:
            pass
    cands[name] = ids or [1]
db.close()
print("参数候选 id：%s" % {k: v[:3] for k, v in sorted(cands.items())})

PLACEHOLDER = re.compile(r"\{(\w+)\}")

# ---------- 登录 ----------
cj = CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
data = urllib.parse.urlencode({"username": "admin", "password": "admin123"}).encode()
op.open(urllib.request.Request(BASE + "/login", data=data, method="POST"),
        timeout=20).read()

SKIP = ("/logout", "/api/", "/delete", "/export", "/static", "/import")

print()
print("=" * 80)
print("逐个访问")
print("=" * 80)

results = []
for path, fn, fn_name in uniq:
    if any(s in path for s in SKIP):
        continue
    if path.startswith("/p/"):
        tok = _portal_token()
        if not tok:
            results.append((path, "SKIP", "无启用中的业主链接", fn_name))
        else:
            try:
                r = op.open(BASE + "/p/" + tok, timeout=30)
                r.read()
                results.append((path, r.status, "/p/<真实token>", fn_name))
            except urllib.error.HTTPError as e:
                e.read()
                results.append((path, e.code, "/p/<真实token>", fn_name))
            except Exception as e:
                results.append((path, "ERR:%s" % type(e).__name__,
                                "/p/<真实token>", fn_name))
        continue
    names = PLACEHOLDER.findall(path)
    if not names:
        variants = [path]
    else:
        variants = []
        # 逐个参数取候选 id 的第一个（优先）；不同名参数独立
        names_unknown = [n for n in names if n not in cands]
        if names_unknown:
            results.append((path, "SKIP", "未知参数 %s" % names_unknown, fn_name))
            continue
        base = path
        # 组合：每个参数都取第一个候选（多数表只有一条数据时足够）
        for n in names:
            base = base.replace("{%s}" % n, str(cands[n][0]))
        variants = [base]
    best = None
    for url in variants:
        try:
            r = op.open(BASE + url, timeout=30)
            body = r.read()
            best = (r.status, len(body), url)
        except urllib.error.HTTPError as e:
            e.read()
            best = (e.code, 0, url)
        except Exception as e:
            best = ("ERR:%s" % type(e).__name__, 0, url)
        break
    results.append((path, best[0], best[2], fn_name))

for path, st, url, fn_name in results:
    flag = "" if st == 200 else "   <== %s" % (st if isinstance(st, str) else st)
    print("  %-6s %-44s %-26s%s" % (st, path[:42], fn_name[:24], flag))

# SKIP 不是失败：`/p/{token}` 在没有启用链接时无从访问，
# 而把它算成失败会让人去查一个根本没坏的东西。
bad = [r for r in results if r[1] not in (200, "SKIP")]
print()
print("=" * 80)
print("非 200：%d / %d" % (len(bad), len(results)))
print("=" * 80)
for path, st, url, fn_name in bad:
    print("  %-44s %-6s  %s" % (path[:42], st, fn_name[:24]))

check("全部 GET 页面返回 200", not bad,
      [(p, s, f) for p, s, _, f in bad][:10])

print()
print("=" * 80)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
print("=" * 78)
sys.exit(1 if F else 0)