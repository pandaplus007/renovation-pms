"""收款侧用词护栏：客户给公司的钱一律说「收」，公司给出去的钱才说「付」。

背景
----
收款节点的状态词曾经是「未付 / 部分 / 已付」，意思却是「客户还没付给我」。
同一个「已付」在这套系统里出现三次且施事方相反：

1. 收款节点 —— 客户 → 公司
2. 供应商付款页（已付 / 剩余未付 / 未付款）—— 公司 → 供应商
3. 业主门户的「已付 ¥x」—— 读者是付款人本人，「你已付」是对的

这不是文风问题，是读代码的人会把 1 和 2 读成同一个方向。
现在收款侧统一为「未收 / 部分已收 / 已收」，付款侧一律不动。

⚠ 两处刻意**不**跟着改，别「顺手统一」：
   - 业主门户的「已付」：读者是客户本人，「你已付」自然；改成「已收」会变成
     「你已收到我的钱」。
   - 「已下单未付款」「剩余未付」等付款侧词：本来就说对了。

所以断言必须写成 `未付(?!款)` 而不是 `未付`——前者排除「未付款」这个
合法的付款侧词。第一版就是漏了这层，报了一个不存在的残留。
"""
import os
import re
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("PMS_BASE", "http://127.0.0.1:8018")
FAILS = []


def check(desc, ok, detail=""):
    print("  [%s] %s" % ("PASS" if ok else "FAIL", desc))
    if not ok:
        if detail:
            print("         %s" % (detail,))
        FAILS.append(desc)


def main():
    print("=" * 78)
    print("[1] 词表本身：收款侧三态必须是「收」，且字典里有对应档位")
    print("=" * 78)
    from app.models import PAYMENT_STATUS
    from app.state_colors import STATE_TONE_MAP

    print("  PAYMENT_STATUS = %s" % PAYMENT_STATUS)
    check("收款节点三态为 未收/部分已收/已收",
          PAYMENT_STATUS == ["未收", "部分已收", "已收"], PAYMENT_STATUS)
    for w in PAYMENT_STATUS:
        check("「%s」在状态色字典内（否则徽章静默回落灰色）" % w,
              w in STATE_TONE_MAP, sorted(STATE_TONE_MAP))
    check("收款侧三态不得复用付款侧的词",
          not ({"未付", "已付", "部分"} & set(PAYMENT_STATUS)),
          PAYMENT_STATUS)

    print()
    print("=" * 78)
    print("[2] 真实渲染：新词在页面上，旧词不在")
    print("=" * 78)
    html = _fetch_project_detail()
    if html is None:
        check("能取到项目详情页", False, "取不到页面，后续断言无意义")
        return
    check("页面可取", True)

    for w in ("已收", "未收", "部分已收"):
        n = html.count(w)
        check("收款侧新词「%s」出现在页面上（%d 次）" % (w, n), n > 0)

    hits = list(re.finditer(r"未付(?!款)", html))
    for m in hits:
        print("         残留于 …%s…" % html[max(0, m.start() - 30):m.end() + 30])
    check("收款侧旧词「未付」不在页面上（已排除合法的「未付款」）",
          not hits, "%d 处残留" % len(hits))

    print()
    print("=" * 78)
    print("[3] 付款侧没被误伤")
    print("=" * 78)
    for w in ("已付款总金额", "已下单未付款"):
        n = html.count(w)
        check("付款侧「%s」原样保留（%d 次）" % (w, n), n > 0)

    print()
    print("=" * 78)
    print("[4] 配色没掉：新词必须真的拿到 state 徽章档位")
    print("=" * 78)
    badges = re.findall(r'badge (state-badge state-(\w+))">([^<]+)</span>', html)
    recv = [(tone, text) for _, tone, text in badges
            if text in ("已收", "未收", "部分已收")]
    print("  收款徽章：%s" % (recv[:8],))
    check("新词拿到了 state 徽章（不是裸文字）", bool(recv))
    tones = {t for t, _ in recv}
    check("徽章档位只落在 idle/watch/done 三档内",
          tones <= {"idle", "watch", "done"}, tones)
    expect = {"已收": "done", "部分已收": "watch", "未收": "idle"}
    for text, want in expect.items():
        got = {t for t, tx in recv if tx == text}
        check("「%s」档位是 %s" % (text, want), got == {want}, got or "页面上没有")


def _fetch_project_detail():
    """登录后取一个有收款节点的详情页；取不到返回 None。"""
    from app.database import SessionLocal
    from app.models import Payment

    db = SessionLocal()
    try:
        rows = db.query(Payment).filter(Payment.void_status == "正常").all()
        if not rows:
            return None
        pid = rows[0].project_id
    finally:
        db.close()

    with httpx.Client(base_url=BASE, timeout=30, follow_redirects=True) as c:
        c.post("/login", data={"username": "admin", "password": "admin123"})
        r = c.get("/projects/%d" % pid)
        if r.status_code != 200:
            return None
        print("  取 /projects/%d -> %d（%d 个收款节点）"
              % (pid, r.status_code, len(rows)))
        return r.text


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("  [FAIL] 脚本自身异常：%r" % (e,))
        FAILS.append("脚本异常")
    print()
    print("=" * 78)
    print("通过 %d 项，失败 %d 项" % (0 if FAILS else 0, len(FAILS))
          if FAILS else "全部通过")
    print("=" * 78)
    sys.exit(1 if FAILS else 0)