"""企业微信群机器人推送。

为什么单独一个模块、而不是在 todo.py 里直接发请求
--------------------------------------------
todo.py 的职责是「扫出哪些事该管」，它是纯函数、不碰网络。加推送要动它
就得引入 IO 与失败处理，两边互相污染。这里改成「todo 负责算，notify 负责发」，
发送端失败绝不影响扫描结果——推送挂了只是没人收到提醒，不该让待办页 500。

失败必须**吞掉并记录**
--------------------
推送是旁路：webhook 超时、限流、群机器人被移除，都会抛异常。若让异常冒到
调用方，表现是「员工点开待办页看到 500」，而这件事本该只是个提醒。
所以这里全部 catch，记一行到 data/notify.log，不影响主流程。

限流：企业微信每个机器人 20 条/分钟。批量推送时自己节流。

配置在 .env：
    WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
留空则整个模块空转（不报错、不发请求），所以没配也能安全引入。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .config import DATA_DIR, WECOM_BASE_URL, WECOM_WEBHOOK

WEBHOOK = WECOM_WEBHOOK.strip()
TIMEOUT = 10          # 秒。设短一点：提醒类消息宁可发失败也别挂住页面
MIN_INTERVAL = 3.0    # 秒。20 条/分钟 ≈ 每条 3 秒，这里留一倍余量
_LOG_LIMIT = 500      # 日志文件保留行数

_last_sent = 0.0


def enabled() -> bool:
    return bool(WEBHOOK)


def _log(msg: str):
    """记录推送失败。日志只留最近若干行，否则会无限增长。"""
    try:
        path = os.path.join(DATA_DIR, "notify.log")
        os.makedirs(DATA_DIR, exist_ok=True)
        lines = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()[-_LOG_LIMIT:]
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {msg}\n")
            for extra in lines[-200:]:
                f.write(extra)
    except Exception:
        pass


def _throttle():
    """节流到 MIN_INTERVAL。返回 False 表示应该跳过这条。"""
    global _last_sent
    now = time.time()
    if now - _last_sent < MIN_INTERVAL:
        return False
    _last_sent = now
    return True


def _post(payload: dict) -> tuple[bool, str]:
    """POST 到 webhook。返回 (成功, 说明)。

    保证**不抛异常**：外层已捕 urllib 的两类异常，但请求体序列化、
    DNS 解析器抛出的其它异常类型（OSError 等）也可能冒出来。
    推送是旁路，绝不能让它把调用方（待办页、定时任务）带崩。
    """
    if not enabled():
        return False, "未配置 WECOM_WEBHOOK"
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            WEBHOOK, data=body,
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
        # 企业微信即使 HTTP 200 也可能 errcode != 0（例如 webhook 被删）
        try:
            data = json.loads(raw)
            if data.get("errcode"):
                return False, f"errcode={data.get('errcode')} {data.get('errmsg', '')}"
        except json.JSONDecodeError:
            pass
        return True, "ok"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"网络错误 {e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _fmt_level(level: str) -> str:
    return {"danger": "🔴", "warning": "🟠", "info": "🔵"}.get(level, "⚪")


def _safe_send(payload: dict, tag: str) -> bool:
    """调 _post 并兜住一切异常。

    `_post` 内部已经捕获网络类异常，但**再包一层**是刻意的：
    monkeypatch、未来的实现改动、或序列化前就有异常时，都可能冒到外面。
    推送是旁路，任何情况下都不能把调用方带崩——所以防线放在调用点，
    而不是依赖被调方「保证不抛」。
    """
    try:
        ok, why = _post(payload)
    except Exception as e:
        _log(f"{tag} 发送异常：{type(e).__name__}: {e}")
        return False
    if not ok:
        _log(f"{tag} 发送失败：{why}")
    return ok


def send_text(text: str) -> bool:
    """发一条纯文本。"""
    if not enabled() or not (text or "").strip():
        return False
    if not _throttle():
        return False
    return _safe_send({"msgtype": "text", "text": {"content": text}}, "text")


def send_alerts(items: list[dict], summary: dict | None = None,
                base_url: str | None = None) -> bool:
    """把待办总览推成一条 markdown 消息。

    刻意**只推 danger 与 warning**：`info` 级（「N 个客户还没有任何跟进记录」）
    每天都一样，推了就是制造噪音，久而久之整个群都会被忽略——这比不推更糟。

    base_url 传外部可访问地址（如 http://192.168.1.100:8018），
    这样手机点消息里的链接能直接打开；留空则不带链接。
    """
    if not enabled():
        return False
    # base_url 传 None 时用配置里的 WECOM_BASE_URL；显式传 "" 表示「这次不带链接」
    base = WECOM_BASE_URL if base_url is None else base_url
    picked = [i for i in (items or []) if i.get("level") in ("danger", "warning")]
    if not picked:
        return False

    danger = sum(1 for i in picked if i.get("level") == "danger")
    warning = len(picked) - danger
    # 用 h2 而非 h3：企业微信 markdown 只支持 # ~ ###### 六级且必须带空格，
    # 写成 "## 标题" 最稳；标题里不放链接，避免整条消息出现两处跳转。
    lines = [f"## 装修待办预警：{danger} 项紧急 / {warning} 项关注", ""]
    for it in picked[:12]:
        # 走「扁平 markdown」而不是标准 markdown 的 <sub>：
        # 标准 markdown 只支持 font_color / 引用 / 部分标题，
        # <sub> 会被原样当文本显示出来，看着像坏了。
        line = (f"- {_fmt_level(it.get('level', ''))} **{it.get('kind', '')}**："
                f"{it.get('title', '')}")
        detail = (it.get("detail") or "").strip()
        if detail:
            # 用 `>` 引用行而不是 <sub>：企业微信 markdown **只支持**
            # 标题、引用、加粗、链接、字体颜色，<sub> 会被原样当文本打印出来，
            # 消息里就会看到一串 "<sub>合同额…</sub>"，看着像坏了。
            lines.append(line)
            lines.append(f"  > {detail}")
            continue
        lines.append(line)
    if len(picked) > 12:
        lines.append(f"- ……另有 {len(picked) - 12} 项，见待办总览")
    if base:
        # base 若带尾部斜杠会拼出「//todo」，部分客户端会当外链处理
        lines += ["", f"[打开待办总览]({base.rstrip('/')}/todo)"]

    return _safe_send({"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}},
                      "alerts")


def test_connection() -> tuple[bool, str]:
    """配置页的「测试推送」用。返回 (成功, 说明)。"""
    if not enabled():
        return False, "未配置 WECOM_WEBHOOK"
    ok = _safe_send({"msgtype": "text",
                     "text": {"content": "装修项目管理系统：推送通道测试成功。"}},
                    "test")
    return (True, "已发送，请到群里查看") if ok else (False, "发送失败，详见 data/notify.log")
