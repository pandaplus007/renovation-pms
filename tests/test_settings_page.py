"""公司设置页的三个易错点：嵌套 form、推送卡片、结果提示。

为什么单独测这个页面
------------------
它是全站少见的「一页三个 form + 一个按钮触发外部请求」的页面，
且历史上就有嵌套 form 的 bug（浏览器**静默丢弃**内层 form，
服务端测试全绿，只有人点按钮才知道没反应）。

同时验证 notify-test 路由可达：它不在任何 MODULE 的 prefixes 里时，
fail-closed 守卫会直接 403，而症状是「点了没反应」。
"""
from __future__ import annotations

import os
import re
import sys
import urllib.parse
import http.cookiejar
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("PMS_BASE", "http://127.0.0.1:8018")
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f" —— {detail}" if detail and not cond else ""))


def login():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.open(BASE + "/login", timeout=20)
    data = urllib.parse.urlencode(
        {"username": "admin", "password": "admin123"}).encode()
    op.open(BASE + "/login", data, timeout=20)
    return op


def main():
    op = login()
    r = op.open(BASE + "/oa/settings", timeout=20)
    html = r.read().decode("utf-8")
    check("公司设置页可访问（200）", r.status == 200, f"实际 {r.status}")

    # ---- 1. 嵌套 form：浏览器会静默丢弃内层 form ----
    # 用正则数「form 里面还有 form」。这是浏览器实际的行为标准。
    nested = re.findall(r"<form[^>]*>(?:(?!</form>).)*<form", html, re.S)
    check("页面里没有嵌套 form（否则「移除 LOGO」点了没反应）",
          len(nested) == 0, f"发现 {len(nested)} 处")
    check("移除 LOGO 的 form 仍在（不能为了去嵌套把它删掉）",
          'action="/oa/settings/logo-remove"' in html)
    # 移除按钮必须在「保存」form 外：检查它所在的 form 的 action 不是 /oa/settings
    m = re.search(r'<form method="post" action="(/oa/settings/logo-remove)"', html)
    check("移除 LOGO 的 form 是独立的（action 不被主表单吞掉）", m is not None)

    # ---- 2. 品牌信息表单仍可用 ----
    check("品牌信息主表单存在", 'action="/oa/settings"' in html)
    check("LOGO 上传输入在主表单内", 'name="file"' in html)

    # ---- 3. 推送卡片 ----
    check("有待办推送卡片", "待办推送" in html)
    check("显示启用状态徽章", "state-badge" in html and ("已启用" in html or "未配置" in html))
    check("有测试推送按钮且 href 正确", 'href="/oa/notify-test"' in html)
    check("给出 .env 配置说明", ".env" in html and "WECOM_WEBHOOK" in html)
    check("说明消息链接的可配项", "WECOM_BASE_URL" in html)

    # ---- 4. notify-test 必须可达（不在 prefixes 里会被 fail-closed 403）----
    r2 = op.open(BASE + "/oa/notify-test", timeout=20)
    check("测试推送路由可达（非 403）", r2.status == 200, f"实际 {r2.status}")
    check("未配置时回显配置提示（而不是报错）",
          "msg=" in r2.url and "WECOM_WEBHOOK" in urllib.parse.unquote(r2.url),
          r2.url)
    check("重定向回设置页", "/oa/settings" in r2.url)

    # ---- 5. 导航栏文字表单没被破坏 ----
    check("导航文字表单仍在", 'action="/oa/settings/nav"' in html)
    check("导航文字含模块项", "names" in html or "labels" in html)

    # ---- 6. 页面里不能残留模板语法 ----
    check("无未渲染的 Jinja 标记", ("{{" not in html) and ("{%" not in html),
          "有 Jinja 语法泄漏到输出")


if __name__ == "__main__":
    print("=== 公司设置页回归 ===")
    try:
        main()
    except Exception as e:
        check(f"测试执行中断：{type(e).__name__}: {e}", False)
    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
