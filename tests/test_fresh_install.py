"""全新数据库启动自检：确认 create_all 与原生迁移两条路都能建出完整表结构。

为什么要单独测：本地库的表都是**历史演进**出来的，早已存在，
所以任何"新建表"的代码路径（create_all、裸 CREATE TABLE）从未被执行过。
只有部署到一台**空机器**时才会第一次跑到——那时候才炸，就晚了。
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="pms_fresh_")
DB = os.path.join(TMP, "fresh.db")

# ⚠ 必须在 `import app.database` **之前**改 DATABASE_URL。
#   database.py 在模块顶层就 `create_engine(DATABASE_URL)`，
#   之后再改 config 里的值只会让 config 与 engine 指向两个不同的库——
#   本地库会被建表，本临时库一表没有，而断言全部查临时库，
#   症状是「所有表都缺」，看起来像迁移彻底坏了。
from app import config
config.DB_PATH = DB
config.DATABASE_URL = f"sqlite:///{DB}"
config.DATA_DIR = TMP
print("目标库：", config.DATABASE_URL)

from app.database import _migrate, engine  # noqa: E402
from app.models import Base  # noqa: E402

_migrate()

EXPECT = [
    "project_portal_links",
    "users",              # data_scope
    "projects",           # owner_user_id
    "tasks",              # assignee_user_id / created_at
    "supplier_payments",  # status / void_reason / voided_at
    "cost_items",
    "payments",
    "payment_receipts",
]
EXTRA = {
    "users": ["data_scope"],
    "projects": ["owner_user_id"],
    "tasks": ["assignee_user_id", "created_at"],
    "supplier_payments": ["status", "void_reason", "voided_at"],
    "cost_items": ["status", "void_reason", "voided_at"],
    "payments": ["status", "void_reason", "voided_at"],
    "payment_receipts": ["status"],
}

ok = True
con = sqlite3.connect(DB)
tables = {r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
print("\n表：")
for t in EXPECT:
    hit = t in tables
    print(("  [OK]  " if hit else "  [缺]  ") + t)
    if not hit:
        ok = False

print("\n列：")
for t, cols in EXTRA.items():
    if t not in tables:
        continue
    have = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
    for c in cols:
        hit = c in have
        print(("  [OK]  " if hit else "  [缺]  ") + f"{t}.{c}")
        if not hit:
            ok = False

# 业主链接的唯一约束：token 必须 UNIQUE，否则「一个 token 对应两个项目」
# 只能靠运气避免。这条在老库上是 ALTER 建不出来的（表已存在），必须靠
# create_all 建表时带上——所以只能在**空库**上验证。
try:
    con.execute("INSERT INTO project_portal_links (project_id, token, enabled) "
                "VALUES (1, 'pmt-same', 0)")
    con.execute("INSERT INTO project_portal_links (project_id, token, enabled) "
                "VALUES (2, 'pmt-same', 0)")
    print("  [缺]  project_portal_links.token 缺 UNIQUE 约束")
    ok = False
except sqlite3.IntegrityError:
    print("  [OK]  project_portal_links.token 有 UNIQUE 约束")

try:
    con.execute("INSERT INTO project_portal_links (project_id, token, enabled) "
                "VALUES (1, 'pmt-a', 0)")
    con.execute("INSERT INTO project_portal_links (project_id, token, enabled) "
                "VALUES (1, 'pmt-b', 0)")
    print("  [缺]  project_portal_links.project_id 缺 UNIQUE 约束（一项目两链接）")
    ok = False
except sqlite3.IntegrityError:
    print("  [OK]  project_portal_links.project_id 有 UNIQUE 约束（一项目一链接）")

con.close()
engine.dispose()
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全新库启动自检通过" if ok else "全新库启动自检失败"))
sys.exit(0 if ok else 1)
