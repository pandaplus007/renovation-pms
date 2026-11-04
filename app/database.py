"""数据库引擎与会话管理（SQLAlchemy 2.0 同步模式）。"""
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATA_DIR, DATABASE_URL, ensure_data_files


class Base(DeclarativeBase):
    pass


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


_ensure_dir()
ensure_data_files()

engine = create_engine(
    DATABASE_URL,
    # timeout=30 秒 = SQLite 的 busy_timeout：拿不到写锁时**排队等待**，
    # 而不是立刻抛 `database is locked`。见下方 _sqlite_concurrency 的说明。
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _sqlite_concurrency(dbapi_conn, _conn_record):
    """每条新连接上设置并发相关 PRAGMA。

    为什么必须做：SQLite 默认的 rollback journal 在**写事务期间锁住整个库**，
    一个人提交时所有人的读也被挡住。而这个系统的典型场景就是办公室几个人
    下午一起录发票、登付款——撞上就是白页 500，员工不敢用，退回微信群 + Excel。

    三个 PRAGMA 各解决一件事，缺一不可：

    - ``journal_mode=WAL``：读写并发，多人同时用得动的前提。
    - ``busy_timeout``：抢不到锁时等待而非立刻抛错。默认 5 秒偏短，
      办公室集中点「保存」很容易超过；给到 30 秒后重试基本都能挤进去。
    - ``synchronous=NORMAL``：WAL 模式下每次提交都 fsync，NORMAL 改成只在
      checkpoint 时 fsync。断电最多丢最后几笔未 checkpoint 的事务（秒级），
      换来的是写入吞吐的数量级差别。**这依赖 WAL 生效**，所以必须放在其后。

    ⚠ WAL 在网络文件系统（SMB / NFS / 某些 virtiofs）上**静默失败**：
    `PRAGMA journal_mode=WAL` 会原样返回原模式而不报错。所以在
    `report_sqlite_mode()` 里校验实际生效值，不靠「设过了就一定成」。

    这里**不开** ``PRAGMA foreign_keys``：全库删除都是物理 `db.delete()`，
    开了外键约束会把「删掉仍有子记录的合同」这类操作从静默成功变成 500。
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA busy_timeout=30000")
        # 顺序有讲究：journal_mode 必须在 synchronous 之前设，否则 NORMAL
        # 是在旧模式下生效的，读回值仍是旧的 delete
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass  # 文件系统不支持 WAL 等情况不阻断启动，降级回默认模式
    finally:
        cur.close()


def report_sqlite_mode() -> str:
    """读回**实际生效**的日志模式，供启动日志与健康检查使用。

    不能相信「PRAGMA 设置过了」——WAL 在不支持的文件系统上不报错也不生效，
    只有读回值才知道。所以启动时打印一行，运维一眼能看出并发加固是否真的落地。
    """
    try:
        with engine.connect() as conn:
            mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
            busy = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
        if str(mode).lower() == "wal":
            # PRAGMA busy_timeout 返回的是**毫秒**，直接打印会得到
            # 「busy_timeout=30000s」这种把 30 秒说成 8 小时的错值，
            # 运维照着它去调参数会越调越离谱。
            return f"WAL（并发读写已开启，等待锁 {int(busy or 0) / 1000:g} 秒）"
        return (f"{mode}（⚠ 非 WAL，多人同时录入可能报 database is locked；"
                "常见于数据库放在网络盘上，需移到本地磁盘）")
    except Exception as e:
        return f"无法读取（{e}）"


def get_db():
    """FastAPI 依赖：请求级数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate():
    """轻量迁移：建缺失表 + 新增列 ALTER TABLE。"""
    Base.metadata.create_all(bind=engine)
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE purchase_contracts ADD COLUMN no_delivery BOOLEAN DEFAULT 0"))
    except Exception:
        pass  # 列已存在
    for _t, _c in [("invoices", "subcontract_id"),
                   ("supplier_payments", "subcontract_id"),
                   ("purchase_orders", "subcontract_id"),
                   ("invoices", "suborder_id"),
                   ("supplier_payments", "suborder_id"),
                   # 任务挂到施工阶段下（工程管理页改版）。存量行为 NULL，
                   # 表示「未排期」，不猜它属于哪个阶段。
                   ("gantt_tasks", "phase_id")]:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE {_t} ADD COLUMN {_c} INTEGER"))
        except Exception:
            pass  # 列已存在
    for _t, _c in [("sub_contracts", "party_a"), ("sub_contracts", "a_tax_no"),
                   ("sub_contracts", "a_address"), ("sub_contracts", "a_contact"),
                   ("sub_contracts", "a_bank"), ("sub_contracts", "a_account"),
                   ("sub_contracts", "b_tax_no"), ("sub_contracts", "b_bank"),
                   ("sub_contracts", "b_account"), ("sub_contracts", "duration_days"),
                   ("sub_contracts", "file_path"),
                   ("contracts", "settle_amount"),
                   ("delivery_notes", "project_id"), ("delivery_notes", "po_id"),
                   ("delivery_notes", "invoice_id"), ("delivery_notes", "payment_id"),
                   ("materials", "contract_id"), ("materials", "opening_stock")]:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE {_t} ADD COLUMN {_c} INTEGER"))
        except Exception:
            pass  # 列已存在
    # 变更签证扫描件的上传留痕。file_path 早就有了，但一直没接上传，
    # 而且没有原始文件名与上传人/时间——事后回溯说不出「哪份、谁传的」。
    # file_uploaded_at 要 DATETIME，SQLite 的 ALTER 不支持带类型的默认值，
    # 只能先加 INTEGER（SQLite 会按动态类型存），由模型负责转 datetime。
    for _c, _ty in [("file_name", "TEXT DEFAULT ''"),
                    ("file_uploaded_at", "DATETIME"),
                    ("file_uploaded_by", "TEXT DEFAULT ''"),
                    # 变更签证工作流（Procore 式）
                    ("submitted_by", "TEXT DEFAULT ''"),
                    ("submitted_at", "DATETIME"),
                    ("approved_by", "TEXT DEFAULT ''"),
                    ("pricing_note", "TEXT DEFAULT ''"),
                    ("reject_reason", "TEXT DEFAULT ''"),
                    ("settled_by", "TEXT DEFAULT ''"),
                    ("linked_po_ids", "TEXT DEFAULT '[]'"),
                    ("linked_sc_ids", "TEXT DEFAULT '[]'"),
                    ("linked_cost_ids", "TEXT DEFAULT '[]'")]:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE change_orders ADD COLUMN {_c} {_ty}"))
        except Exception:
            pass  # 列已存在
    # 审批流程：未审批不入账开关
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE approval_flows ADD COLUMN block_booking BOOLEAN DEFAULT 0"))
    except Exception:
        pass  # 列已存在
    # 行级权限：项目创建者（users 外键）+ 用户的数据范围。
    #
    # ⚠ 两列都要回填，理由不同但同样致命：
    #   - projects.owner_user_id 保持 NULL 是**正确**的（无从推断谁建的），
    #     所以查询里一律配 is_(None) 兜底，不能让 NULL 行集体消失。
    #   - users.data_scope 必须回填成 'all'。NULL 会让 normalize() 落到
    #     'none'（fail-closed），表现为「升级后所有员工突然看不到任何项目」——
    #     这是最坏的一种升级事故：所有人都以为系统坏了。
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE projects ADD COLUMN owner_user_id INTEGER"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_projects_owner_user_id ON projects(owner_user_id)"))
    except Exception:
        pass
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN data_scope VARCHAR(12) DEFAULT 'all'"))
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET data_scope='all' "
                              "WHERE data_scope IS NULL OR data_scope=''"))
    except Exception:
        pass
    # 业主只读链接。create_all 会建表，这里无需 ALTER；
    # 但 token 必须是 UNIQUE + 有索引，否则「同一个 token 对应两个项目」
    # 这种事只能靠运气避免。
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS project_portal_links ("
                "id INTEGER PRIMARY KEY,"
                "project_id INTEGER NOT NULL UNIQUE,"
                "token VARCHAR(64) NOT NULL UNIQUE,"
                "enabled BOOLEAN DEFAULT 0,"
                "show_photos BOOLEAN DEFAULT 0,"
                "show_payment BOOLEAN DEFAULT 0,"
                "expires_at DATETIME,"
                "view_count INTEGER DEFAULT 0,"
                "last_viewed_at DATETIME,"
                "created_at DATETIME)"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_portal_token "
                "ON project_portal_links(token)"))
    except Exception:
        pass
    # 任务：负责人外键（「我的任务」要用）+ 创建时间。
    # 历史行 assignee_user_id 为 NULL 是**正常**的——那些任务是自由文本录入的，
    # 无从推断该指向谁。判据里用 is_(None) 兜住，且「我的任务」查不到它们，
    # 这符合预期：没绑定账号的任务本就不知道该算谁的。
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN assignee_user_id INTEGER"))
            conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN created_at DATETIME"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_tasks_assignee_user_id ON tasks(assignee_user_id)"))
        # created_at 回填：ALTER 加的列在旧行上是 NULL，而排序会按它排，
        # NULL 在 SQLite 的排序里位置不固定，表现为「列表顺序每次刷新都变」。
        with engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET created_at=datetime('now','localtime') "
                              "WHERE created_at IS NULL"))
    except Exception:
        pass
    # 子任务：parent_id 指向同表。旧行 NULL = 顶层任务，不需要回填。
    # ⚠ 必须单独一个 try：上面那个 try 里第一条 ALTER 在已有库上就会抛
    #   「duplicate column」，整块被跳过——新列永远加不上，而且不报错。
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN parent_id INTEGER"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_tasks_parent_id ON tasks(parent_id)"))
    except Exception:
        pass  # 列已存在
    # 材料当前库存 stock 列（期初 + 入库 - 出库）
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE materials ADD COLUMN stock FLOAT DEFAULT 0"))
    except Exception:
        pass  # 列已存在
    # 材料发票退税：不含税价 + 退税状态
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE invoices ADD COLUMN tax_excl FLOAT DEFAULT 0"))
    except Exception:
        pass
    # 发票 → 开票申请单的反向关联（销项专用，见 Invoice.app_id 注释）
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE invoices ADD COLUMN app_id INTEGER"))
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_invoices_app_id ON invoices(app_id)"))
    except Exception:
        pass
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE invoices ADD COLUMN refund_status VARCHAR(30) DEFAULT ''"))
    except Exception:
        pass
    # 收款明细扩展字段（付款人/收款人/账号/开户行/用途）
    for _c in ("payer", "payee", "payer_account", "payee_account",
               "payer_bank", "payee_bank", "purpose"):
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE payment_receipts ADD COLUMN {_c} VARCHAR(200) DEFAULT ''"))
        except Exception:
            pass
    # 看板拖拽：甘特任务要有状态（看板列位）与列内排序。
    # status 刻意给 '未开始' 而不是 ''，让历史行迁移后直接落在第一列，
    # 而不需要在模板里为「空状态」再开一列。
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE gantt_tasks ADD COLUMN status VARCHAR(20) DEFAULT '未开始'"))
    except Exception:
        pass
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE gantt_tasks ADD COLUMN sort_order INTEGER DEFAULT 0"))
    except Exception:
        pass
    # 作废制（软删除）：付款与费用不再物理删除，改为标记作废。
    #
    # 收款侧（payments / payment_receipts）同批做废。收款节点尤其要作废化：
    # receipts 是 cascade="all, delete-orphan"，删节点会连带删掉它下面
    # 所有收款凭证和已上传的转账单——一次误操作就丢法律凭据。
    #
    # 关键是**必须回填**：ALTER TABLE 加的列在已有行上是 NULL，而 SQL 里
    # `NULL != '作废'` 结果是 NULL（假），于是 filter(status != '作废')
    # 会把全部历史行过滤掉——症状是「升级后付款/费用列表集体变空」，
    # 且没有任何报错，极易被误判成数据丢了。所以下面无条件 UPDATE 一次。
    for _t in ("supplier_payments", "cost_items", "payments", "payment_receipts"):
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE {_t} ADD COLUMN status VARCHAR(10) DEFAULT '正常'"))
        except Exception:
            pass  # 列已存在
        for _c, _typ in (("void_reason", "VARCHAR(200) DEFAULT ''"),
                         ("voided_at", "DATETIME")):
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {_t} ADD COLUMN {_c} {_typ}"))
            except Exception:
                pass  # 列已存在
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"UPDATE {_t} SET status='正常' WHERE status IS NULL OR status=''"))
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS ix_{_t}_status ON {_t}(status)"))
        except Exception:
            pass
    # ALTER 不会回填已有行，这些行在新列上是 NULL。
    # status 为 NULL 会让 state_badge 静默回落 idle，看起来是「未开始」但语义上是
    # 「不知道」——两种在 SQLite 上还不一样（NULL 落到看板哪一列不可控）。
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE gantt_tasks SET status='未开始' WHERE status IS NULL OR status=''"))
    except Exception:
        pass
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE gantt_tasks SET sort_order=0 WHERE sort_order IS NULL"))
    except Exception:
        pass

    # 账号会话版本号：改密码后作废旧会话
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN pwd_ver INTEGER DEFAULT 1"))
    except Exception:
        pass
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET pwd_ver = 1 WHERE pwd_ver IS NULL"))
    except Exception:
        pass


_migrate()


def _seed_permissions():
    """首次启动：建立岗位表并把旧版「岗位→模块可见性」矩阵播种为完整权限。

    播种时给全量动作位（view/create/edit/delete/export/approve），
    使迁移前后每个岗位的行为完全一致；上线后由 OA 权限矩阵页按需收紧。
    已存在的行不会被覆盖，所以老板在界面上改过的配置不会丢。
    """
    from .models import Role, RoleModulePerm
    from .permissions import (ALL_ACTIONS_MASK, ALL_MODULES,
                              APPROVAL_DEFAULT_PERMS, APPROVAL_MODULE,
                              BUILTIN_ROLES, DEFAULT_PERMS_EXTRA,
                              LEGACY_ROLE_MODULES, PAYABLES_DEFAULT_PERMS,
                              PAYABLES_MODULE, invalidate_perm_cache)
    try:
        with SessionLocal() as db:
            for i, name in enumerate(BUILTIN_ROLES):
                if not db.query(Role).filter(Role.name == name).first():
                    db.add(Role(name=name, builtin=True, sort_order=(i + 1) * 10))
            db.commit()

            existing = {(r.role, r.module) for r in db.query(RoleModulePerm).all()}

            # 先算出「本轮要插入的行」，再统一去重后写入。
            # 不能边遍历边 insert：LEGACY_ROLE_MODULES 的「老板」= ALL_MODULES 已含 approvals，
            # 会与 APPROVAL_DEFAULT_PERMS 的 (老板, approvals) 撞唯一键，
            # 异常会让整个函数回滚——结果是新模块一条权限都没种上。
            wanted: dict[tuple[str, str], int] = {}
            for role, modules in LEGACY_ROLE_MODULES.items():
                for mod in modules & ALL_MODULES:
                    if (role, mod) not in existing:
                        wanted[(role, mod)] = ALL_ACTIONS_MASK
            for role, flags in APPROVAL_DEFAULT_PERMS.items():
                key = (role, APPROVAL_MODULE)
                if key not in existing:
                    # 差异化审批权限优先于通用全量
                    wanted[key] = flags
            for role, flags in PAYABLES_DEFAULT_PERMS.items():
                key = (role, PAYABLES_MODULE)
                if key not in existing:
                    wanted[key] = flags
            # 后加的三个模块（变更签证 / 报价单 / 开票申请）同理由单独播种
            for _mod, _perms in DEFAULT_PERMS_EXTRA.items():
                for role, flags in _perms.items():
                    key = (role, _mod)
                    if key not in existing:
                        wanted[key] = flags

            appr_added = sum(1 for (r, m) in wanted if m == APPROVAL_MODULE)
            ap_added = sum(1 for (r, m) in wanted if m == PAYABLES_MODULE)
            extra_added = sum(1 for (r, m) in wanted if m in DEFAULT_PERMS_EXTRA)
            for (role, mod), flags in wanted.items():
                db.add(RoleModulePerm(role=role, module=mod, flags=flags))
            if wanted:
                db.commit()
                print(f"[permissions] 已播种 {len(wanted)} 条岗位权限"
                      f"（审批中心 {appr_added}、应付账款 {ap_added}、"
                      f"变更/报价/开票 {extra_added}；迁移前行为保持不变）")
            invalidate_perm_cache()
    except Exception as e:
        print(f"[permissions] 播种失败：{e}")


def _backfill_stock():
    """一次性回填材料的当前库存（stock = 期初 + 入库 - 出库）。"""
    try:
        from sqlalchemy import func
        from .models import InventoryRecord, Material
        with SessionLocal() as db:
            rows = db.query(InventoryRecord.material_id, InventoryRecord.record_type,
                            func.coalesce(func.sum(InventoryRecord.quantity), 0.0)).group_by(
                            InventoryRecord.material_id, InventoryRecord.record_type).all()
            by_key = {}
            for mid, rtype, qty in rows:
                by_key[(mid, rtype)] = float(qty or 0)
            for m in db.query(Material).all():
                _in = by_key.get((m.id, "入库"), 0.0)
                _out = by_key.get((m.id, "出库"), 0.0)
                m.stock = round((m.opening_stock or 0.0) + _in - _out, 2)
            db.commit()
    except Exception:
        pass


_backfill_stock()


def backup_db():
    """备份数据库到 data/backups/，保留最近 30 份；复制后校验完整性。

    ⚠ 这里**不能**用 shutil.copy2 复制主 db 文件。

    开 WAL 之后，已提交的事务可能还留在 `renovation.db-wal` 里没并入主文件，
    直接复制主文件得到的是一个「少了最后几笔账」的快照——而下面的 quick_check
    照样返回 ok，属于最坏的一类假绿：备份看起来成功，真要回滚时才发现缺数据。

    改用 SQLite 自带的 backup API（`src.backup(dst)`）：它先取写锁做一次
    checkpoint 再逐页拷贝，产出的目标文件是自洽的单文件快照，不含 -wal/-shm。
    """
    import os
    import sqlite3
    from datetime import datetime
    try:
        backup_dir = os.path.join(DATA_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        src = os.path.join(DATA_DIR, "renovation.db")
        if not os.path.exists(src):
            return
        name = "renovation_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".db"
        dest = os.path.join(backup_dir, name)
        # snapshot API：目标是一次一致快照，不需要停服
        s = sqlite3.connect(src, timeout=60)
        try:
            d = sqlite3.connect(dest)
            try:
                s.backup(d)
            finally:
                d.close()
        finally:
            s.close()
        # 完整性校验：复制后能打开且 quick_check 通过
        try:
            conn = sqlite3.connect(dest)
            res = conn.execute("PRAGMA quick_check").fetchone()
            conn.close()
            if not res or res[0] != "ok":
                os.remove(dest)
                _log_backup_error(name, f"quick_check 失败: {res}")
                return
        except Exception as e:
            _log_backup_error(name, f"校验异常: {e}")
            return
        # 清理旧备份
        files = sorted(os.listdir(backup_dir))
        while len(files) > 30:
            os.remove(os.path.join(backup_dir, files[0]))
            files.pop(0)
    except Exception:
        pass


def _log_backup_error(name: str, reason: str):
    """记录备份失败告警到 data/backups/backup_errors.log。"""
    import os
    from datetime import datetime
    try:
        backup_dir = os.path.join(DATA_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        log_path = os.path.join(backup_dir, "backup_errors.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {name} {reason}\n")
    except Exception:
        pass
