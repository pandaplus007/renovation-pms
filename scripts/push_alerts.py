"""待办推送：把 todo.collect() 的紧急项发到企业微信群。

单独一个脚本而不是并进 daily_alerts.py：两者推送频次不同
（每日预警报告 vs 待办预警），且本脚本要能被 crontab 直接调用。

用法：
    python scripts/push_alerts.py            # 正常推送
    python scripts/push_alerts.py --dry      # 只打印不发（先看会推什么）

设计要点：
- 未配置 WECOM_WEBHOOK 时安静退出（exit 0），不报错——
  没配推送的人不该每天在 crontab 日志里看到红色失败。
- 退出码：0 = 已推送 / 无需推送 / 未启用；1 = 推送失败。
  crontab 只关心「有没有推成功」。
"""
from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import notify                      # noqa: E402
from app.database import SessionLocal       # noqa: E402
from app.todo import collect                # noqa: E402


def main(dry: bool = False) -> int:
    if not notify.enabled():
        print("未配置 WECOM_WEBHOOK，跳过推送。")
        print("配置方法见「公司设置 → 待办推送」。")
        return 0

    db = SessionLocal()
    try:
        items = collect(db)
    finally:
        db.close()

    picked = [i for i in items if i.get("level") in ("danger", "warning")]
    if not picked:
        print("当前没有紧急或关注级待办，不推送（避免每天发一条「一切正常」的噪音）。")
        return 0

    danger = sum(1 for i in picked if i.get("level") == "danger")
    print(f"待办：{len(items)} 项，其中 {danger} 紧急 / {len(picked) - danger} 关注")

    if dry:
        for it in picked:
            print(f"  [{it['level']}] {it['kind']}：{it['title']} — {it['detail']}")
        print("(dry-run，未实际发送)")
        return 0

    ok = notify.send_alerts(items)
    print("推送成功。" if ok else "推送失败，原因见 data/notify.log")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(dry="--dry" in sys.argv))
