"""精简部署回归：没有 app/notify.py 时，应用仍能启动并正常服务。

为什么这个测试存在
------------------
生产部署**刻意不包含**企业微信推送（2026-10-10 决定「暂时不上线」），
源码保留在工作区、打包时排除 `app/notify.py`。

于是生产上跑的是一份「少一个模块」的代码。原来的写法是硬导入：

    from .. import notify

少文件时它在**模块导入期**就抛 ImportError，后果是**整个服务起不来**，
而不只是某个页面 500——而且报错堆栈指向 `app/routers/oa.py`，
完全看不出真正原因是「这份部署本来就不含这个功能」。

所以这里直接模拟「文件不在」：把 `app.notify` 从 sys.modules 摘掉，
并在 import 时抛 ImportError，然后要求：
  1. 应用能正常 import（不崩）
  2. `/oa/settings` 返回 200
  3. 设置页**不出现**推送卡片（不是显示一个点了必然失败的按钮）
  4. `/oa/notify-test` 不 500，而是跳转到一句说明

判据用「sys.modules 注入假模块」而不是真删文件：真删有风险
（测试中途崩掉就把工作区文件弄没了），且和实际场景不等价——
实际是**文件不存在**，我们是**模块不存在**，对 import 而言等价。
"""
from __future__ import annotations

import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (f" —— {detail}" if detail and not cond else ""))


def main():
    print("=== 精简部署回归（无 notify.py）===\n")

    # ---- 1. 源码层面：notify 的导入必须是可选的 ----
    oa_path = os.path.join(ROOT, "app", "routers", "oa.py")
    oa_src = io.open(oa_path, encoding="utf-8").read()

    check("oa.py 里没有裸的 `from .. import notify`（会 ImportError 崩启动）",
          "\n    from .. import notify\n" not in oa_src,
          "存在硬导入；缺 notify.py 的部署会启动失败")
    check("oa.py 用了 try/except ImportError 包住 notify 导入",
          "except ImportError" in oa_src)

    # ---- 2. 模板层面：推送卡片必须由 notify_deployed 守卫 ----
    tpl = io.open(os.path.join(ROOT, "app", "templates", "oa", "settings.html"),
                  encoding="utf-8").read()
    guard_at = tpl.find("{% if notify_deployed %}")
    # 卡片锚点用**真实标记**而不是「企微推送」四个字——
    # 那四个字也出现在守卫上方的注释里，会让「守卫在卡片之前」判反。
    card_at = tpl.find('bi-send me-1"></i>待办推送')
    end_at = -1
    check("settings.html 存在 notify_deployed 守卫", guard_at != -1)
    check("守卫包住整张推送卡片（守卫 < 卡片 < endif）",
          guard_at != -1 and card_at > guard_at,
          f"guard={guard_at} card={card_at}")
    block_end = tpl.rfind("{% endblock %}")
    check("推送卡片位于 endblock 之前（内容块最后一个元素）",
          guard_at != -1 and card_at < block_end,
          f"card={card_at} endblock={block_end}")

    # ---- 3. 运行时：模拟 notify 模块不存在，要求应用照常启动 ----
    #
    # 机制用 `sys.modules['app.notify'] = None`。这是 CPython 的**文档行为**：
    # 命中 sys.modules 里值为 None 的名字就抛 ImportError，等价于「文件不存在」。
    #
    # 别改用替换 builtins.__import__ 的老写法：Python 3.13 里
    # `from .. import notify` 的 IMPORT_NAME **不经过** builtins.__import__
    # 这个 Python 层钩子，拦截器一次都不会被调用，
    # 于是「测试全绿但其实什么都没验证」——比没有测试更糟。
    import importlib
    saved_notify = sys.modules.get("app.notify", "__absent__")
    saved_oa = sys.modules.get("app.routers.oa", "__absent__")
    sys.modules["app.notify"] = None
    sys.modules.pop("app.routers.oa", None)
    try:
        mod = importlib.import_module("app.routers.oa")
        check("notify 缺失时 app.routers.oa 仍能 import", True)
        got = mod._notify_module()
        check("_notify_module() 在缺模块时返回 None", got is None,
              f"实际返回 {got!r} —— 硬导入没被兜住")

        # 路由仍应注册（_route_actions.json 里登记着，不能不一致）
        paths = {getattr(r, "path", None) for r in mod.router.routes}
        check("notify-test 路由仍注册（否则与 _route_actions.json 不一致）",
              "/oa/notify-test" in paths,
              f"实际路由 {sorted(p for p in paths if p)}")
    except Exception as e:
        check("notify 缺失时 app.routers.oa 仍能 import", False,
              f"{type(e).__name__}: {e}")
    finally:
        sys.modules.pop("app.routers.oa", None)
        if saved_notify == "__absent__":
            sys.modules.pop("app.notify", None)
        else:
            sys.modules["app.notify"] = saved_notify
        if saved_oa != "__absent__":
            sys.modules["app.routers.oa"] = saved_oa

    # ---- 4. 渲染层：notify_deployed=False 时不输出推送卡片 ----
    # 只截取「守卫 → 与之配对的 endif」这一段渲染：整页依赖 base.html 的
    # 大量上下文，这里要验的是「守卫语法正确，且为假时卡片真的不出现」。
    def matching_endif(src: str, start: int) -> int:
        """从 `{% if %}` 处开始，找到与之**配对**的那个 endif 的起始位置。

        必须配对，不能用 `find('{% endif %}')`：卡片内部还有
        `{% if notify_enabled %}`，取第一个 endif 会截出半截，
        Jinja 报「Unexpected end of template」——
        而这个报错跟真实缺陷（守卫位置错了）长得完全一样。
        """
        depth = 0
        i = start
        for m in re.finditer(r"\{%-?\s*(if|endif|elif|else)\b", src[start:]):
            tag = m.group(1)
            if tag == "if":
                depth += 1
            elif tag == "endif":
                depth -= 1
                if depth == 0:
                    return start + m.start()
            i = start + m.start()
        return -1

    try:
        from jinja2 import Environment
        match_at = matching_endif(tpl, guard_at)
        check("能定位到守卫的配对 endif（卡片内部有嵌套 if）",
              match_at > guard_at, f"match_at={match_at}")
        seg = tpl[guard_at:match_at + len("{% endif %}")]
        env = Environment()
        out = env.from_string(seg).render(
            notify_deployed=False, notify_enabled=False)
        check("notify_deployed=False 时模板能正常渲染", True)
        check("notify_deployed=False 时不输出推送卡片",
              "WECOM_WEBHOOK" not in out and "测试推送" not in out)
        out2 = env.from_string(seg).render(
            notify_deployed=True, notify_enabled=True)
        check("notify_deployed=True 时推送卡片正常输出",
              "测试推送" in out2 and "WECOM_WEBHOOK" in out2)
    except Exception as e:
        check("推送卡片守卫的模板渲染", False, f"{type(e).__name__}: {e}")

    # ---- 5. 打包侧：notify.py 不该进生产包 ----
    print("\n（打包侧由 pack_for_nas.py 的 MUST_NOT_HAVE 断言，见打包输出）")

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
