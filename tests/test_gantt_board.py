"""看板拖拽端到端验证。

跑法：先起服务，再 `python tests/test_gantt_board.py`（可带端口 `... 8019`）。
默认连 8018，与 run.py 的默认端口一致。

为什么必须有这层：拖拽有两个只在真实交互下才暴露的失败模式，
服务端渲染测试都抓不到 ——

  1. **跨列拖动没落库**。reorder 第一版只查 `status == 目标列` 当成员，
     拖进来的新任务根本不在集合里，被静默跳过。DOM 已经移过去了、
     界面显示成功，刷新一下弹回原列。只测「列内换序」永远测不出来，
     必须测「跨列拖动」。
  2. **模板 500**。看板不传 colors，而图例块裸访问 colors.items()。
     这是 AGENTS.md 里「同一个模板被多个路由渲染时上下文键要一致」那条，
     症状是 GET /gantt?mode=board 直接 500。

夹具清理用 atexit（中途抛异常时裸语句会被跳过，脏数据留库）。
断言按 id 比，不写绝对值——库里还有种子任务，硬数总数会误报。
"""
import atexit
import os
import sqlite3
import sys

import requests

PORT = sys.argv[1] if len(sys.argv) > 1 else "8018"
BASE = f"http://127.0.0.1:{PORT}"
MARK = "__kanban_e2e__"

# 数据库按脚本自身位置定位，不靠 cwd。从仓库根跑和从 tests/ 跑都得能用——
# 否则换个目录就报「no such table」，症状离根因很远。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "renovation.db")

created = []
seed_snapshot = {}
s = requests.Session()


def cleanup():
    """必须用 atexit 登记：中途抛异常时裸语句会被跳过，夹具留在库里。

    顺序不能反：先删夹具、再还原种子。还原时按 id 写回，夹具已不在表里，
    两者不会互相干扰；但先删更符合「夹具是本测试的产物」的直觉。
    """
    if created:
        n = 0
        for tid in created:
            try:
                r = s.post(f"{BASE}/gantt/tasks/{tid}/delete", timeout=10,
                           allow_redirects=False)
                # 302/303 = 成功；200 也算（重定向被跟随）
                n += 1 if r.status_code in (200, 302, 303) else 0
            except Exception as e:
                print(f"  清理 {tid} 失败: {e}")
        print(f"清理：提交删除 {n}/{len(created)} 条")
    try:
        restore_fixtures(seed_snapshot)
    except Exception as e:
        print(f"  还原种子任务失败: {e}")


atexit.register(cleanup)


def check(label, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {extra}" if extra else ""))
    return cond


def db_state():
    """直接读库断言落库，不依赖页面回显。

    只查带 MARK 的夹具 —— 库里还有 seed.py 建的种子任务。
    """
    con = sqlite3.connect(DB)
    rows = con.execute(
        f"SELECT id, name, status, sort_order FROM gantt_tasks WHERE name LIKE ? ORDER BY id",
        (f"%{MARK}%",)).fetchall()
    con.close()
    return {r[0]: {"name": r[1], "status": r[2], "sort": r[3]} for r in rows}


def snapshot_fixtures():
    """跑之前把夹具列里的其它任务（种子数据）的 status/sort_order 存下来。

    为什么必须还原：reorder 的设计是「目标列里没被提交的任务接在末尾」，
    而拖夹具时提交的是**夹具那一列的完整顺序**——种子任务和夹具同列，
    就会被一起重新编号。功能上没错（那正是它该做的），但测试不该在
    真实库里留下痕迹：跑一次测试，种子任务的排序就变了。
    """
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, status, sort_order FROM gantt_tasks WHERE name NOT LIKE ?",
        (f"%{MARK}%",)).fetchall()
    con.close()
    return {r[0]: (r[1], r[2]) for r in rows}


def restore_fixtures(snap):
    """把 snapshot_fixtures 记下的状态原样写回。"""
    if not snap:
        return
    con = sqlite3.connect(DB)
    for tid, (status, sort) in snap.items():
        con.execute("UPDATE gantt_tasks SET status=?, sort_order=? WHERE id=?",
                    (status, sort, tid))
    con.commit()
    con.close()
    print(f"还原种子任务 {len(snap)} 条")


failed = 0
try:
    # ---------- 0. 前置检查 + 快照 ----------
    try:
        s.get(f"{BASE}/login", timeout=5)
    except requests.exceptions.ConnectionError:
        print(f"\n连不上 {BASE}。先起服务，例如：\n"
              f"    python run.py {PORT}\n"
              f"（端口不一致就带参数跑：python tests/test_gantt_board.py <端口>）")
        sys.exit(2)

    # 拖夹具会连带重排同列的种子任务，跑完必须还原（见 snapshot_fixtures）
    seed_snapshot = snapshot_fixtures()
    print(f"快照：种子任务 {len(seed_snapshot)} 条（跑完还原）")
    try:
        s.get(f"{BASE}/login", timeout=5)
    except requests.exceptions.ConnectionError:
        print(f"\n连不上 {BASE}。先起服务，例如：\n"
              f"    python run.py {PORT}\n"
              f"（端口不一致就带参数跑：python tests/test_gantt_board.py <端口>）")
        sys.exit(2)

    # ---------- 1. 登录 ----------
    r = s.post(f"{BASE}/login", data={"username": "admin", "password": "admin123"},
               timeout=10, allow_redirects=False)
    failed += not check("登录", r.status_code in (200, 302, 303), f"HTTP {r.status_code}")

    # ---------- 2. 建任务 ----------
    for i, (nm, st) in enumerate([("A", "未开始"), ("B", "未开始"), ("C", "未开始")]):
        r = s.post(f"{BASE}/gantt/tasks",
                   data={"name": f"{MARK}_{nm}", "start_date": "2026-10-01",
                         "end_date": "2026-10-10", "assignee": f"工{i}",
                         "project_id": "0", "note": ""},
                   timeout=10, allow_redirects=False)
        failed += not check(f"建任务 {nm}", r.status_code in (302, 303), f"HTTP {r.status_code}")
    state = db_state()
    created = sorted(state)
    failed += not check("落库 3 条", len(created) == 3, f"实际 {len(created)}")
    if len(created) != 3:
        sys.exit(1)
    a, b, c = created
    failed += not check("初始状态=未开始",
                        all(state[i]["status"] == "未开始" for i in created))

    # ---------- 3. 看板渲染 ----------
    r = s.get(f"{BASE}/gantt?mode=board", timeout=10)
    html = r.content.decode("utf-8", "replace")
    failed += not check("GET /gantt?mode=board 200", r.status_code == 200, f"HTTP {r.status_code}")
    failed += not check("三列都在", all(f'data-status="{c}"' in html
                                         for c in ("未开始", "进行中", "已完成")))
    failed += not check("拖拽端点已挂上", 'data-reorder-url="/gantt/tasks/reorder"' in html)
    # 模板区块插错位置的经典症状：代码在文件里但内容没渲染。
    # 只数自己建的夹具卡片 —— 库里还有种子任务，硬数总数会误报。
    n_cards = html.count(MARK)
    failed += not check("夹具卡片已渲染", n_cards >= 3, f"实际 {n_cards}")
    # state_badge 必须命中字典，不能全落 idle
    failed += not check("未开始徽章=idle",
                        'state-badge state-idle">未开始' in html)

    # ---------- 4. reorder：跨列 + 列内换序 ----------
    # 把 a 移到「进行中」，并让 b、c 在第一列换序为 c、b
    r = s.post(f"{BASE}/gantt/tasks/reorder",
               data={"status": "未开始", "order": f"{c},{b}"},
               timeout=10, allow_redirects=False)
    failed += not check("reorder 未开始列", r.status_code in (302, 303), f"HTTP {r.status_code}")
    state = db_state()
    failed += not check("c.sort=0", state[c]["sort"] == 0, f"实际 {state[c]['sort']}")
    failed += not check("b.sort=1", state[b]["sort"] == 1, f"实际 {state[b]['sort']}")

    r = s.post(f"{BASE}/gantt/tasks/reorder",
               data={"status": "进行中", "order": f"{a}"},
               timeout=10, allow_redirects=False)
    failed += not check("reorder 移入进行中", r.status_code in (302, 303), f"HTTP {r.status_code}")
    state = db_state()
    failed += not check("a.status=进行中", state[a]["status"] == "进行中",
                        f"实际 {state[a]['status']}")
    failed += not check("a 从第一列消失",
                        state[b]["status"] == "未开始" and state[c]["status"] == "未开始")

    # 看板页此时应显示 a 在进行中列
    html = s.get(f"{BASE}/gantt?mode=board", timeout=10).content.decode("utf-8", "replace")
    seg = html.split('data-status="进行中"')[1].split('data-status=')[0] if 'data-status="进行中"' in html else ""
    failed += not check("a 出现在进行中列", f"{MARK}_A" in seg)
    failed += not check("b、c 仍在未开始列", f"{MARK}_B" in html and f"{MARK}_C" in html)

    # ---------- 5. 幂等：重复提交同一条列表不应累积偏移 ----------
    before = db_state()
    for _ in range(3):
        s.post(f"{BASE}/gantt/tasks/reorder",
               data={"status": "进行中", "order": f"{a}"},
               timeout=10, allow_redirects=False)
    after = db_state()
    failed += not check("重复提交幂等", before[a]["sort"] == after[a]["sort"],
                        f"{before[a]['sort']} -> {after[a]['sort']}")

    # ---------- 6. status 端点（非拖拽入口） ----------
    r = s.post(f"{BASE}/gantt/tasks/{c}/status", data={"status": "已完成"},
               timeout=10, allow_redirects=False)
    failed += not check("status 端点", r.status_code in (302, 303), f"HTTP {r.status_code}")
    state = db_state()
    failed += not check("c.status=已完成", state[c]["status"] == "已完成",
                        f"实际 {state[c]['status']}")

    # ---------- 7. 容错 ----------
    r = s.post(f"{BASE}/gantt/tasks/reorder",
               data={"status": "不存在的列", "order": str(a)},
               timeout=10, allow_redirects=False)
    failed += not check("非法 status → 400/403", r.status_code in (400, 403),
                        f"HTTP {r.status_code}")

    r = s.post(f"{BASE}/gantt/tasks/reorder",
               data={"status": "未开始", "order": "999999"},
               timeout=10, allow_redirects=False)
    failed += not check("不存在的 id 不炸", r.status_code in (302, 303), f"HTTP {r.status_code}")

    r = s.post(f"{BASE}/gantt/tasks/reorder",
               data={"status": "未开始", "order": "abc,def"},
               timeout=10, allow_redirects=False)
    failed += not check("非数字 id → 400", r.status_code in (400, 403), f"HTTP {r.status_code}")

    # 非法 status 不该改动任何行
    state_after = db_state()
    failed += not check("非法请求未污染数据",
                        state_after[a]["status"] == "进行中"
                        and state_after[c]["status"] == "已完成")

    # ---------- 8. 另两个模式未被打破 ----------
    for m in ("timeline", "gantt"):
        rr = s.get(f"{BASE}/gantt?mode={m}", timeout=10)
        failed += not check(f"GET mode={m} 仍 200", rr.status_code == 200, f"HTTP {rr.status_code}")

except Exception as e:
    print(f"\n异常中断: {type(e).__name__}: {e}")
    failed += 1
finally:
    cleanup()

print()
print(f"失败 {failed} 项")
sys.exit(1 if failed else 0)