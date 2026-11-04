"""部署配置自检：`config.py` 读的每个环境变量，compose 都必须声明。

为什么需要这个测试
------------------
本项目的配置有个**双重陷阱**，两边任一环节漏了都表现为「配了但没生效」，
而且**全程零报错**：

1. `.dockerignore` 里有 `.env` → `/app/.env` 不在镜像里；
   compose 也没把它挂进容器 → 容器内 `config._load_dotenv()` 读到空就跳过。
2. `docker compose` 的 `.env` **只用于 `${VAR}` 插值**，
   不会把里面的变量自动注入容器。

所以唯一生效的路径是：`config.py` 里 `os.getenv("X")` 的每个 X，
都必须在 `docker-compose.yml` 的 `environment:` 里出现一行 `- X=${X:-}`。

这不是假想——本轮加 `PORTAL_BASE_URL` / `WECOM_WEBHOOK` 时就漏了，
症状是业主链接指向内网地址且毫无报错。

为什么用「解析 YAML 文本」而不是 yaml 库
----------------------------------------
不引入 pyyaml 依赖（这个项目没有）。文件结构是固定的列表项，
用正则逐行匹配足够，且不装依赖能在任何环境跑。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = os.path.join(ROOT, "docker-compose.yml")
DOCKERIGNORE = os.path.join(ROOT, ".dockerignore")
CONFIG = os.path.join(ROOT, "app", "config.py")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (f" —— {detail}" if detail and not cond else ""))


def declared_env_vars() -> set[str]:
    """从 compose 的 environment: 段里取出声明过的变量名。

    只认 `- NAME=` 形式（当前文件全是这种）。写成 `environment: {NAME: x}`
    的 map 形式会漏掉——但那种写法现在没用，出现时本测试会因「变量没声明」
    而报错，等于提醒改回来。
    """
    out = set()
    with open(COMPOSE, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\s*-\s*([A-Z][A-Z0-9_]*)\s*=", line)
            if m:
                out.add(m.group(1))
    return out


def config_env_vars() -> set[str]:
    """所有真正被 os.getenv 读取的变量名。

    扫的是**整个 app/ 加 run.py**，不只是 config.py：
    `HOST` 是在 run.py 里读的，不在 config.py，漏掉它会让
    「compose 无多余声明」这条误报。
    """
    out = set()
    targets = [CONFIG]
    app_dir = os.path.join(ROOT, "app")
    for dirpath, dirnames, filenames in os.walk(app_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                targets.append(os.path.join(dirpath, fn))
    targets.append(os.path.join(ROOT, "run.py"))
    rx = re.compile(r'os\.getenv\(\s*["\']([A-Z][A-Z0-9_]*)["\']'
                    r'|os\.environ\.get\(\s*["\']([A-Z][A-Z0-9_]*)["\']'
                    r'|os\.environ\[\s*["\']([A-Z][A-Z0-9_]*)["\']')
    for path in targets:
        with open(path, encoding="utf-8") as f:
            for m in rx.finditer(f.read()):
                out.add(m.group(1) or m.group(2) or m.group(3))
    return out


def main():
    print("=== 部署配置自检 ===\n")

    declared = declared_env_vars()
    needed = config_env_vars()

    print(f"compose 声明：{len(declared)} 个")
    print(f"config 读取 ：{len(needed)} 个\n")

    missing = sorted(needed - declared)
    check("config 读取的每个环境变量都在 compose 中声明（配了才生效）",
          not missing,
          f"漏声明 {missing} —— 这些变量写进 .env 也不会进容器，且不报错")

    for v in sorted(needed):
        mark = "OK " if v in declared else "缺 "
        print(f"    [{mark}] {v}")

    # compose 里声明了但没有任何代码读的 → 多余声明。
    # `PORT` 例外：它只出现在 `${PORT:-8018}` 的端口映射里，
    # 由 compose  自己消费，不进容器，代码里当然读不到。
    extra = sorted(declared - needed - {"PORT"})
    check("compose 无多余声明（避免留下以为生效了的假配置）",
          not extra, f"多余 {extra}")

    # ---- 记录这条陷阱本身，避免有人「优化」掉 .env 的支持 ----
    with open(DOCKERIGNORE, encoding="utf-8") as f:
        ignored = {ln.strip() for ln in f}
    check(".env 被 .dockerignore 排除（已知陷阱，见本文件头注释）",
          ".env" in ignored,
          ".env 若进了镜像，内容会被硬编码进镜像层 —— 反过来若不在，"
          "容器内 _load_dotenv() 读到空。两种都要清楚，别随手改")

    # ---- PORTAL_BASE_URL 的默认值必须为空，不能是内网地址 ----
    # 若哪天有人把内网地址写成默认值，业主链接会「看起来正常但打不开」，
    # 而管理页的 localhost 警告也不会亮（它只判断 127.0.0.1 / localhost）。
    with open(COMPOSE, encoding="utf-8") as f:
        compose_text = f.read()
    m = re.search(r"-\s*PORTAL_BASE_URL=(\S*)", compose_text)
    check("PORTAL_BASE_URL 在 compose 中的默认值是空",
          m is not None and m.group(1) == "${PORTAL_BASE_URL:-}",
          f"实际默认值 = {m.group(1) if m else '(未找到)'}")

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
