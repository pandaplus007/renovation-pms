"""回款比例（recv_pct）的正确性验证。

这个数字要人照着账本核，所以口径必须写死在测试里，而不是「和页面上看到的一致」
就算过——那样测的是渲染不是算术。

要验的性质：
  1. 口径正确 —— 手算一遍和属性算出来的一致（含「无合同返回 None」）
  2. 边界不崩 —— 合同为 0 / 无收款 / 超收 100%+ / 单个收款节点为 0
  3. 没有把施工进度污染成回款进度 —— 两个数必须独立
  4. 页面上真的分成了两列，且标签写的是「回款」「施工」而不是含混的「进度」
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.database import SessionLocal  # noqa: E402
from app.models import Payment, Project  # noqa: E402

P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


db = SessionLocal()
try:
    projects = db.query(Project).order_by(Project.id).all()

    print("=" * 78)
    print("[1] 口径：手算 已收÷合同额，与属性比对")
    print("=" * 78)
    print("  %-4s %-24s %14s %14s %8s %8s" % ("id", "name", "合同额", "已收", "手算", "属性"))
    for p in projects:
        amt = p.contract.amount if p.contract else 0
        got = sum(x.paid_amount for x in p.payments)
        hand = (got / amt * 100) if amt else None
        prop = p.recv_pct
        print("  %-4s %-24s %14s %14s %8s %8s"
              % (p.id, p.name[:22], amt, got,
                 ("%.1f" % hand) if hand is not None else "None",
                 ("%.1f" % prop) if prop is not None else "None"))
        check("#%d 属性等于手算" % p.id,
              (hand is None and prop is None)
              or (hand is not None and prop is not None
                  and abs(hand - prop) < 1e-9),
              (hand, prop))
        check("#%d recv_amount 等于逐笔求和" % p.id,
              abs(p.recv_amount - got) < 1e-6, (p.recv_amount, got))

    print()
    print("=" * 78)
    print("[2] 边界：不该崩、不该给出误导性的 0")
    print("=" * 78)
    p_no_contract = next((p for p in projects if not p.contract), None)
    if p_no_contract:
        check("无合同项目返回 None 而不是 0（0% 会被读成没收钱）",
              p_no_contract.recv_pct is None, p_no_contract.recv_pct)
    else:
        print("  （数据里每个项目都有合同，跳过无合同分支）")

    p_zero = next((p for p in projects
                   if p.contract and not p.contract.amount), None)
    if p_zero:
        check("合同额为 0 返回 None（不能除零）",
              p_zero.recv_pct is None, p_zero.recv_pct)
    else:
        print("  （没有合同额为 0 的合同，跳过除零分支）")

    p_over = next((p for p in projects
                   if p.contract and p.contract.amount
                   and p.recv_amount > p.contract.amount), None)
    if p_over:
        pct = p_over.recv_pct
        check("超收时比例 > 100（不截断，超收本身是要看见的信息）",
              pct is not None and pct > 100, pct)
    else:
        print("  （当前无超收项目）")

    p_zero_pay = next((p for p in projects
                       if p.contract and p.contract.amount
                       and p.recv_amount == 0), None)
    if p_zero_pay:
        check("有合同但零收款时为 0.0（这里 0 是真的 0，与无合同不同）",
              p_zero_pay.recv_pct == 0.0, p_zero_pay.recv_pct)
    else:
        print("  （没有零收款的有合同项目）")

    print()
    print("=" * 78)
    print("[3] 两个进度互不污染")
    print("=" * 78)
    # 改施工进度不该动回款比例，反之亦然。
    # 注意第一版这里取错了变量：拿 before_pct = p.progress 去比 p.recv_pct，
    # 两者本就是不同口径，比出来必然不等——测试红是因为写错了，不是代码错。
    p = projects[0]
    before_recv_pct = p.recv_pct
    before_recv_amt = p.recv_amount
    before_build = p.progress
    p.progress = 77
    check("改施工进度不影响回款比例",
          abs(p.recv_pct - before_recv_pct) < 1e-9,
          (before_recv_pct, p.recv_pct))
    check("改施工进度不影响已收合计",
          abs(p.recv_amount - before_recv_amt) < 1e-6,
          (before_recv_amt, p.recv_amount))
    check("progress 仍可独立写入（未被 recv_pct 顶掉）", p.progress == 77,
          p.progress)
    db.rollback()
    db.refresh(p)
    check("回滚后施工进度复原", p.progress == before_build,
          (before_build, p.progress))
    check("回滚后回款比例不变",
          abs(p.recv_pct - before_recv_pct) < 1e-9,
          (before_recv_pct, p.recv_pct))

    # 页面上不得再出现含混的「进度」列头
    print()
    print("=" * 78)
    print("[4] 页面标签：两个进度都写明了是哪个")
    print("=" * 78)
    TPL = os.path.join(ROOT, "app", "templates")
    for rel in ["dashboard.html", os.path.join("projects", "list.html"),
                os.path.join("projects", "detail.html"),
                os.path.join("customers", "detail.html")]:
        s = io.open(os.path.join(TPL, rel), encoding="utf-8",
                    errors="replace").read()
        bare = re.findall(r"<th[^>]*>\s*进度\s*</th>", s)
        has_recv = ("回款" in s)
        print("    %-28s 含混的「进度」列头 %d 处，有「回款」%s"
              % (rel.replace("\\", "/"), len(bare), has_recv))
        check("%-28s 列头不含混的「进度」" % rel.replace("\\", "/"),
              not bare, bare)

    print()
    print("=" * 78)
    print("[5] 裸 progress 显示是否都有防护（不防护就会显示恒 0 的施工进度）")
    print("=" * 78)
    # 不是「一处都不许出现」。分三类：
    #   无关      d.progress = 采购/分包的**下单进度**（已下单 ÷ 合同额，
    #             见 routers/subcontracts.py:69 的 ordered/c.amount），
    #             与项目施工进度毫无关系，只是恰好同名。
    #   有防护    施工进度本身该显示，但必须在 {% if p.phases %} 分支里——
    #             两个项目都没建阶段，无条件显示 0% 会被读成「一点没干」。
    #   无防护    该判失败。
    # 第一版这里只查「有没有出现」，把前两类一起判成违规。
    UNRELATED = ("d",)          # 采购/分包下单进度，对象名不同但字段同名
    TAG = re.compile(r"\{%-?\s*(if|for|endif|endfor)\b([^%]*)%\}?")
    BARE, GUARDED, UNREL = [], [], []
    for dirpath, _, files in os.walk(TPL):
        for fn in files:
            if not fn.endswith(".html") or fn == "_progress.html":
                continue
            path = os.path.join(dirpath, fn)
            s = io.open(path, encoding="utf-8", errors="replace").read()
            # 用栈跟踪 Jinja 作用域：扫描到某偏移量时，栈里是它所嵌套的全部
            # {% if %} 条件。之前「逐行往上数深度」的做法在 if 与 endif
            # 写在同一行时会立刻抵消归零，反而漏判有防护的那两处。
            events = [(m.start(), "tag", (m.group(1), m.group(2)))
                      for m in TAG.finditer(s)]
            events += [(m.start(), "ref", m.group(1))
                       for m in re.finditer(r"\{\{\s*([\w.]+)\.progress\s*\}\}", s)]
            events.sort(key=lambda e: e[0])
            # 标签与引用必须**按偏移量混在一起**顺序处理。之前先扫标签、
            # 再回头捕获「位置在前」的引用，导致引用被它后面那个标签捕获，
            # 用的是已经弹栈的状态——同一行 {% if %}…{% endif %} 里的引用
            # 就会被误判成无防护。
            stack, refs_at = [], []
            for pos, kind, payload in events:
                if kind == "tag":
                    if payload[0] in ("if", "for"):
                        stack.append(payload[1])
                    elif stack:
                        stack.pop()
                else:
                    refs_at.append((pos, payload, list(stack)))
            for pos, obj, enclosing in refs_at:
                ln = s[:pos].count("\n") + 1
                item = (os.path.relpath(path, TPL), ln, obj)
                if obj in UNRELATED:
                    UNREL.append(item)
                    continue
                if any("phases" in cond for cond in enclosing):
                    GUARDED.append(item)
                else:
                    BARE.append((item, enclosing))
    print("    无关对象（下单进度，非项目进度）：%d 处" % len(UNREL))
    for rel, ln, obj in UNREL:
        print("      %-26s %4d  %s.progress" % (rel[:26], ln, obj))
    print("    有阶段防护的施工进度：%d 处" % len(GUARDED))
    for rel, ln, obj in GUARDED:
        print("      %-26s %4d  %s.progress" % (rel[:26], ln, obj))
    print("    无防护的裸输出：%d 处" % len(BARE))
    for item, enclosing in BARE:
        rel, ln, obj = item
        print("    ! %-26s %4d  %s.progress  外层条件=%s"
              % (rel[:26], ln, obj, enclosing))
    check("没有无防护的裸 progress 输出（%d 处）" % len(BARE),
          not BARE, BARE[:4])
finally:
    db.rollback()
    db.close()

print()
print("=" * 78)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
print("=" * 78)
sys.exit(1 if F else 0)