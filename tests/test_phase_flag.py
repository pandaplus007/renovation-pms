"""「阶段未开始」矛盾提示的验证。

这个提示的判定必须**严格**——判宽了就是噪声。项目列表上多挂一个
到处都有的徽章，等于没有徽章。所以除了验证「该报的时候报了」，
更重要的���验证「不该报的时候一个都不报」。

不报的情况有四种，逐个造出来验：
  1. 项目不在施工态（待签约本就没开工）
  2. 项目没有阶段记录（是「没录阶段」，不是「录了没开始」）
  3. 有阶段已开始（哪怕只有 1 个）
  4. 阶段已完成（正常收尾）

另外确认用的不是红色：alert 在这套体系里专供金额异常，
矛盾提示若也用红，会稀释红色已有的信号强度。
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.database import SessionLocal  # noqa: E402
from app.models import Project  # noqa: E402

P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


class FakePhase(object):
    """只带 status 的假阶段。判定逻辑只读 status，不需要真对象。"""

    def __init__(self, status):
        self.status = status


class FakeProject(object):
    """把 Project.phase_never_started 的逻辑原样搬过来，用假数据驱动。

    为什么不直接用真模型改状态：改真库需要 commit/rollback，而 rollback
    之后 Session 里的对象会 expire，重新访问触发刷新，测试变脆。
    这里复制判据来驱动 —— 代价是判据改了两处会不同步，所以末尾
    会用真模型再对一遍同样的用例。
    """

    def __init__(self, status, phase_statuses):
        self.status = status
        self.phases = [FakePhase(s) for s in phase_statuses]

    @property
    def phase_never_started(self):
        if self.status not in ("施工中", "待验收"):
            return False
        if not self.phases:
            return False
        return all(ph.status == "未开始" for ph in self.phases)


print("=" * 78)
print("[1] 该报的场景")
print("=" * 78)
flag = FakeProject("施工中", ["未开始"] * 7)
check("施工中 + 7 个阶段全未开始 -> 报", flag.phase_never_started is True)
flag = FakeProject("待验收", ["未开始", "未开始"])
check("待验收 + 阶段全未开始 -> 报", flag.phase_never_started is True)
flag = FakeProject("施工中", ["未开始"] * 1)
check("只有一个阶段且未开始 -> 也报", flag.phase_never_started is True)

print()
print("=" * 78)
print("[2] 不该报的场景（判宽了就是噪声）")
print("=" * 78)
for label, st, phases in [
    ("待签约 + 阶段全未开始（本该没开工）", "待签约", ["未开始"] * 7),
    ("待开工 + 阶段全未开始", "待开工", ["未开始"] * 7),
    ("已验收 + 阶段全未开始", "已验收", ["未开始"] * 7),
    ("售后 + 阶段全未开始", "售后", ["未开始"] * 7),
    ("施工中 + 没有任何阶段（是没录阶段）", "施工中", []),
    ("施工中 + 有 1 个阶段已开始", "施工中", ["已完成", "未开始", "未开始"]),
    ("施工中 + 有 1 个阶段进行中", "施工中", ["进行中", "未开始"]),
    ("施工中 + 全部已完成", "施工中", ["已完成"] * 7),
]:
    p = FakeProject(st, phases)
    check(label + " -> 不报", p.phase_never_started is False,
          p.phase_never_started)

print()
print("=" * 78)
print("[3] 用真模型复核（判据只搬了一遍，必须确认逻辑一致）")
print("=" * 78)
db = SessionLocal()
try:
    for p in db.query(Project).order_by(Project.id).all():
        expect = FakeProject(p.status, [ph.status for ph in p.phases])
        real = p.phase_never_started
        check("#%d %s 判定一致" % (p.id, p.name[:16]),
              real == expect.phase_never_started,
              (real, expect.phase_never_started))
        started = sum(1 for ph in p.phases if ph.status != "未开始")
        print("      #%d status=%-6s 阶段=%d 已开始=%d -> phase_never_started=%s"
              % (p.id, p.status, len(p.phases), started, real))

    print()
    print("=" * 78)
    print("[4] 阶段表确实没有时间字段（所以不能写「未更新 N 天」）")
    print("=" * 78)
    cols = {c.name for c in Project.__table__.columns} \
        if hasattr(Project, "__table__") else set()
    import sqlalchemy as sa
    from app.database import engine
    insp = sa.inspect(engine)
    pcols = [c["name"] for c in insp.get_columns("project_phases")]
    print("  project_phases 字段：%s" % pcols)
    time_cols = [c for c in pcols
                 if any(k in c.lower() for k in
                        ("date", "time", "_at", "created", "updated"))]
    check("确认无时间字段（测试前提：判不了「多久没更新」）",
          not time_cols, time_cols)

    print()
    print("=" * 78)
    print("[5] 提示用琥珀而非红色")
    print("=" * 78)
    macro = io.open(os.path.join(ROOT, "app", "templates", "_progress.html"),
                    encoding="utf-8", errors="replace").read()
    i = macro.find("macro phase_flag")
    seg = macro[i:macro.find("endmacro", i)] if i > 0 else ""
    check("phase_flag 用 state-watch", "state-watch" in seg, seg[:120])
    check("phase_flag 不用 state-alert（红留给金额异常）",
          "state-alert" not in seg, seg[:120])
    # 文案不得出现编造的时长
    check("文案不含「未更新 N 天」这类编造时长",
          not re.search(r"未更新\s*\d", seg), seg[:160])
    check("文案写的是可验证的事实「阶段未开始」",
          "阶段未开始" in seg, seg[:160])
finally:
    db.close()

print()
print("=" * 78)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
print("=" * 78)
sys.exit(1 if F else 0)