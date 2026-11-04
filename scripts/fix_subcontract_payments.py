"""分包付款归属修复：把分包合同的乙方建成供应商，并回填付款的 supplier_id。

为什么需要
----------
`supplier_payments.supplier_id` 为 0 的付款，是走分包流程录入的（关联
`subcontract_id`，收款方是分包合同的乙方），而不是走供应商采购流程。结果是：
按供应商汇总时这些钱凭空消失，「这个分包商我一共付了多少」查不出来。

本脚本做的事
------------
1. 扫描分包合同的 `party_b`，在供应商表里按名称匹配（含包含匹配）
2. 匹配不到 → 新建供应商（分类标为「劳务分包」，便于识别）
3. 把该合同下 `supplier_id` 为 0/空的付款，回填为匹配到的供应商 id

安全约束
--------
- **只补空值**：绝不覆盖已有的 supplier_id
- **幂等**：重复运行不会重复建供应商、不会重复改付款
- **dry-run 优先**：默认只报告不写，`--apply` 才真正执行
- 写操作前自动备份 renovation.db

用法：
    python scripts/fix_subcontract_payments.py            # 只报告
    python scripts/fix_subcontract_payments.py --apply     # 执行修复
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "renovation.db")

SUBCONTRACT_CATEGORY = "劳务分包"

# suppliers 表有几列 NOT NULL 且在数据库层没有默认值（ORM 的 default 只在
# SQLAlchemy 插入时生效），用原生 sqlite3 写入必须显式给值，否则报
# NOT NULL constraint failed。
SUPPLIER_REQUIRED = ("contact", "phone", "address", "note", "created_at")


def insert_supplier(cur, name: str, note: str) -> int:
    """新建供应商并返回 id。显式补齐所有 NOT NULL 无默认值的列。"""
    cur.execute(
        "insert into suppliers (name, category, contact, phone, address, note, created_at) "
        "values (?,?,?,?,?,?,?)",
        (name, SUBCONTRACT_CATEGORY, "", "", "", note, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return cur.lastrowid


def norm(s: str) -> str:
    return (s or "").strip().replace(" ", "").replace("　", "")


def match_supplier(name: str, sup_by_norm: dict):
    """按名称匹配供应商。

    返回 (供应商id | None, 匹配方式, 候选列表)。
    匹配方式：exact / contain / none

    刻意**不做**编辑距离式的模糊匹配。真实数据里出现过
    「中俊致飞建筑工程有限公司」与「中骏致飞建筑工程有限公司福田分公司」
    这种只差一字、却是两家公司的名字——自动配对等于把 34 万挂错公司。
    `contain`（一方完整包含另一方）可信度较高，但也只作为候选提示，
    由人确认后再用 --link 指定。
    """
    n = norm(name)
    if not n:
        return None, "empty", []
    if n in sup_by_norm:
        return sup_by_norm[n][0], "exact", []
    cands = []
    for k, ids in sup_by_norm.items():
        if not k:
            continue
        # 只在「一方完整包含另一方」时给候选，不做逐字近似
        if k in n or n in k:
            for sid in ids:
                cands.append((sid, k))
    if cands:
        return None, "contain", cands
    return None, "none", []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只报告）")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--link", action="append", default=[],
                    metavar="合同ID:供应商ID",
                    help="人工指定分包合同→供应商的对应关系，可重复。"
                         "用于处理系统无法确定的近似名称（如 中俊 vs 中骏）")
    ap.add_argument("--create", action="append", default=[], metavar="合同ID",
                    help="强制为指定分包合同新建供应商记录")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("数据库不存在:", args.db)
        return 1

    # 解析人工指定
    manual = {}
    for item in args.link:
        try:
            k, v = item.split(":", 1)
            manual[int(k)] = int(v)
        except ValueError:
            print("--link 格式应为 合同ID:供应商ID，收到:", item)
            return 1
    force_create = {int(x) for x in args.create}

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    print("=" * 72)
    print("分包付款归属修复" + ("（执行模式）" if args.apply else "（只读报告，加 --apply 才写入）"))
    print("=" * 72)

    sups = cur.execute("select id, name, category from suppliers").fetchall()
    sup_by_norm = {}
    sup_name_by_id = {s["id"]: s["name"] for s in sups}
    for s in sups:
        sup_by_norm.setdefault(norm(s["name"]), []).append(s["id"])

    scs = cur.execute(
        "select id, contract_no, name, party_b, amount from sub_contracts").fetchall()

    created = []
    mapping = {}      # subcontract_id -> supplier_id（已确定）
    need_review = []  # 需要人工判断的

    for sc in scs:
        party = sc["party_b"] or ""
        sid, how, cands = match_supplier(party, sup_by_norm)

        if sc["id"] in manual:
            sid, how = manual[sc["id"]], "manual"
        elif sc["id"] in force_create:
            sid, how = None, "force-create"

        if sid:
            mapping[sc["id"]] = sid
        elif sc["id"] in force_create:
            if args.apply:
                note = f"由分包合同 #{sc['id']}（{sc['contract_no'] or sc['name']}）自动创建"
                new_id = insert_supplier(cur, party, note)
                sup_by_norm.setdefault(norm(party), []).append(new_id)
                sup_name_by_id[new_id] = party
                mapping[sc["id"]] = new_id
                created.append((new_id, party))
        elif how == "none" and args.apply and not need_review:
            # 供应商表确实没有这家，且没有近似候选 → 直接新建
            note = f"由分包合同 #{sc['id']}（{sc['contract_no'] or sc['name']}）自动创建"
            new_id = insert_supplier(cur, party, note)
            sup_by_norm.setdefault(norm(party), []).append(new_id)
            sup_name_by_id[new_id] = party
            mapping[sc["id"]] = new_id
            created.append((new_id, party))
        else:
            # contain（有近似候选，但不自动决定）/ none 且未 apply（无法预判）
            # 一律交给人确认，避免把只差一字的名称配成同一家
            need_review.append((sc, cands, how))

    print("\n=== 1. 分包合同乙方匹配结果 ===")
    print("  分包合同 %d 份" % len(scs))
    print("  已确定归属: %d 份" % len(mapping))
    print("  待人工确认: %d 份" % len(need_review))
    if manual:
        print("  人工指定: %s" % ", ".join("合同#%s→供应商#%s" % (k, v) for k, v in manual.items()))

    if need_review:
        print("\n  ⚠ 以下合同未自动关联，需你确认：")
        for sc, cands, how in need_review:
            print("\n    分包合同 #%s  %s" % (sc["id"], sc["name"]))
            print("      合同额 %.0f  乙方「%s」" % (sc["amount"] or 0, sc["party_b"]))
            if cands:
                print("      名称相近的已有供应商（可能是同一家，也可能只差一个字）：")
                for sid, kn in cands:
                    print("        供应商#%-4s %s" % (sid, sup_name_by_id.get(sid, kn)))
                print("      → 确为同一家： --link %d:<供应商ID>" % sc["id"])
            else:
                print("      供应商表里确实没有这家")
                print("      → 确为新公司： --create %d" % sc["id"])

    if created:
        print("\n  已新建供应商:")
        for sid, name in created:
            print("    #%-4s %s" % (sid, name))

    # ---- 回填付款 ----
    print("\n=== 2. 回填付款的 supplier_id ===")
    rows = cur.execute("""
        select sp.id, sp.amount, sp.pay_date, sp.subcontract_id
        from supplier_payments sp
        where (sp.supplier_id is null or sp.supplier_id = 0)
          and sp.subcontract_id is not null
        order by sp.subcontract_id, sp.id
    """).fetchall()

    filled = [(r, mapping[r["subcontract_id"]]) for r in rows if r["subcontract_id"] in mapping]
    skipped = [r for r in rows if r["subcontract_id"] not in mapping]

    print("  待修复付款: %d 笔" % len(rows))
    print("  可回填:     %d 笔，%.2f 元"
          % (len(filled), sum(r["amount"] or 0 for r, _ in filled)))
    print("  待人工确认: %d 笔" % len(skipped))
    for r, sid in filled:
        print("    付款#%-4s %-12s %10.2f  -> %s(#%s)"
              % (r["id"], str(r["pay_date"])[:10], r["amount"] or 0,
                 (sup_name_by_id.get(sid) or "")[:20], sid))
    for r in skipped:
        print("    付款#%-4s %-12s %10.2f  -> 待确认（分包合同#%s）"
              % (r["id"], str(r["pay_date"])[:10], r["amount"] or 0, r["subcontract_id"]))

    # ---- 剩余无归属 ----
    left = cur.execute("""
        select sp.id, sp.amount, sp.pay_date, sp.note, sp.subcontract_id, sp.contract_id
        from supplier_payments sp
        where (sp.supplier_id is null or sp.supplier_id = 0)
    """).fetchall()
    print("\n=== 3. 本次修复后仍将无归属 ===")
    print("  %d 笔，%.2f 元" % (len(left), sum(r["amount"] or 0 for r in left)))
    for r in left:
        print("    #%-4s %-12s %10.2f  分包合同:%s 采购合同:%s  备注:%s"
              % (r["id"], str(r["pay_date"])[:10] if r["pay_date"] else "-",
                 r["amount"] or 0, r["subcontract_id"] or "-", r["contract_id"] or "-",
                 (r["note"] or "")[:24]))

    if not args.apply:
        print("\n" + "=" * 72)
        if need_review:
            print("有 %d 份合同需你确认后，用 --link / --create 指定，再加 --apply 执行。"
                  % len(need_review))
        else:
            print("以上为预览。加 --apply 执行修复。")
        print("=" * 72)
        con.close()
        return 0

    # ---- 备份 ----
    bak = args.db + ".bak-fixsub-" + datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(args.db, bak)
    print("\n[备份] %s" % bak)

    for r, sid in filled:
        cur.execute("update supplier_payments set supplier_id=? where id=?",
                    (sid, r["id"]))
    con.commit()

    print("[写入] 新建供应商 %d 个，回填付款 %d 笔" % (len(created), len(filled)))

    print("\n=== 4. 校验 ===")
    still = cur.execute("""
        select count(*) from supplier_payments
        where (supplier_id is null or supplier_id = 0) and subcontract_id is not null
    """).fetchone()[0]
    print("  分包付款中 supplier_id 为 0 的: %d 笔" % still)
    orphan = cur.execute("""
        select count(*) from supplier_payments sp
        where sp.supplier_id not in (select id from suppliers)
    """).fetchone()[0]
    print("  supplier_id 指向不存在供应商的: %d 笔" % orphan)
    print("  quick_check:", cur.execute("PRAGMA quick_check").fetchone()[0])
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())