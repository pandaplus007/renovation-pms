"""推送模块回归：企微 webhook 未配置 / 配错 / 节流 / 消息体构造。

为什么这些要测
------------
推送是**旁路**，失败必须完全不影响主流程——但正因为失败是静默的，
「静默失败」和「正常工作」在页面上长得一模一样。所以必须测：

1. 没配 webhook 时不抛异常（否则没配的人天天看到报错）
2. webhook 配错（返回 errcode）时不抛异常、且记日志
3. HTTP 失败时不抛异常
4. 节流生效（连发两条，第二条被跳过）
5. info 级不进推送（每天一样的噪音会让整个群被忽略）
6. 消息体是合法 JSON、含预期的 markdown 字段

不真的发网络请求：用 monkeypatch 替换 _post。
"""
from __future__ import annotations

import io
import json
import os
import sys
import atexit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import notify

PASS, FAIL = [], []
CAPTURED = []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f" —— {detail}" if detail and not cond else ""))


def _fake_post(ok=True, why="ok"):
    def _p(payload):
        CAPTURED.append(payload)
        return ok, why
    return _p


def _cleanup():
    """删掉测试期间写下的日志，避免污染 data/notify.log。"""
    try:
        p = os.path.join(notify.DATA_DIR, "notify.log")
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


atexit.register(_cleanup)
_cleanup()

ITEMS = [
    {"level": "danger", "kind": "采购合同超付", "title": "瓷砖采购合同",
     "detail": "合同额 100,000，已付 112,000", "amount": 12000, "count": 1, "url": "/contracts/1"},
    {"level": "warning", "kind": "应收逾期", "title": "领航大厦",
     "detail": "计划 2026-09-01，已逾期 39 天", "amount": 30000, "count": 1, "url": "/projects/1"},
    {"level": "info", "kind": "客户未跟进", "title": "12 个客户没有跟进记录",
     "detail": "线索放着不跟进就不会成交", "amount": 0, "count": 12, "url": "/customers"},
]


def main():
    orig_post = notify._post
    orig_enabled = notify.WEBHOOK

    # ---------- 1. 未配置：安静空转 ----------
    notify.WEBHOOK = ""
    check("未配置时 enabled() 为 False", not notify.enabled())
    check("未配置时 send_text 不抛异常", notify.send_text("hi") is False)
    check("未配置时 send_alerts 不抛异常", notify.send_alerts(ITEMS) is False)
    check("未配置时 test_connection 返回失败+说明",
          notify.test_connection() == (False, "未配置 WECOM_WEBHOOK"))

    # ---------- 2. 正常发送的消息体 ----------
    notify.WEBHOOK = "https://example.invalid/cgi-bin/webhook/send?key=dummy"
    notify._post = _fake_post(ok=True)
    notify._last_sent = 0.0
    CAPTURED.clear()
    check("配置后 enabled() 为 True", notify.enabled())
    check("send_alerts 成功返回 True", notify.send_alerts(ITEMS, base_url="http://1.2.3.4:8018") is True)
    check("实际发出了 1 条消息", len(CAPTURED) == 1, f"实际 {len(CAPTURED)}")

    payload = CAPTURED[0] if CAPTURED else {}
    check("消息类型是 markdown", payload.get("msgtype") == "markdown")
    body = payload.get("markdown", {}).get("content", "")
    check("正文含紧急项标题", "瓷砖采购合同" in body)
    check("正文含关注项标题", "领航大厦" in body)
    check("**正文不含 info 级项**（每天一样的噪音会让人忽略整个群）",
          "客户未跟进" not in body and "12 个客户" not in body)
    check("正文含待办总览链接", "http://1.2.3.4:8018/todo" in body)
    check("链接没有双斜杠（//todo 会被当外链）", "//todo" not in body)
    check("正文不含未支持的 <sub> 标签（企微会原样显示）", "<sub>" not in body)
    check("消息体是合法 JSON", isinstance(json.dumps(payload, ensure_ascii=False), str))

    # ---------- 3. base_url 尾部斜杠 ----------
    CAPTURED.clear()
    notify._last_sent = 0.0
    notify.send_alerts(ITEMS, base_url="http://1.2.3.4:8018/")
    body2 = CAPTURED[0]["markdown"]["content"] if CAPTURED else ""
    check("base_url 带尾部斜杠时链接仍正确", "http://1.2.3.4:8018/todo" in body2
          and "//todo" not in body2)

    # ---------- 4. 节流 ----------
    CAPTURED.clear()
    notify._last_sent = 0.0
    a = notify.send_text("第一条")
    b = notify.send_text("第二条（应被节流跳过）")
    check("第一条发送成功", a is True)
    check("第二条被节流跳过（企业微信限流 20 条/分钟）", b is False)
    check("节流期间只发出 1 条", len(CAPTURED) == 1, f"实际 {len(CAPTURED)}")

    # ---------- 5. 只有 info 级时不推 ----------
    CAPTURED.clear()
    notify._last_sent = 0.0
    info_only = [i for i in ITEMS if i["level"] == "info"]
    check("全是 info 时不推送", notify.send_alerts(info_only) is False)
    check("全是 info 时未发出请求", len(CAPTURED) == 0)
    check("空列表也不推送", notify.send_alerts([]) is False)

    # ---------- 6. 推送失败：不抛异常 + 记日志 ----------
    notify._post = _fake_post(ok=False, why="errcode=93000 invalid webhook url")
    notify._last_sent = 0.0
    try:
        r = notify.send_alerts(ITEMS, base_url="")
        check("webhook 返回 errcode 时不抛异常", True)
        check("失败时返回 False", r is False)
    except Exception as e:
        check("webhook 返回 errcode 时不抛异常", False, f"抛了 {type(e).__name__}: {e}")
    log_path = os.path.join(notify.DATA_DIR, "notify.log")
    check("失败已记入 data/notify.log", os.path.exists(log_path))
    if os.path.exists(log_path):
        log = io.open(log_path, encoding="utf-8").read()
        check("日志含失败原因", "93000" in log)
        check("日志含时间戳（便于排查何时开始坏的）", log.strip().startswith("["))

    # ---------- 7. 底层错误也吞掉 ----------
    def _boom(payload):
        raise OSError("模拟 DNS 解析失败")
    notify._post = _boom
    notify._last_sent = 0.0
    try:
        notify.send_alerts(ITEMS)
        check("底层抛异常时 send_alerts 不外泄", True)
    except Exception as e:
        check("底层抛异常时 send_alerts 不外泄", False, f"抛了 {type(e).__name__}")

    # ---------- 8. 测试推送 ----------
    notify._post = _fake_post(ok=True)
    ok, why = notify.test_connection()
    check("test_connection 成功时返回 True 与提示", ok is True and bool(why))

    notify._post = orig_post
    notify.WEBHOOK = orig_enabled


if __name__ == "__main__":
    print("=== 企微推送回归 ===")
    main()
    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
