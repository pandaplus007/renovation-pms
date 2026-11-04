"""精简部署端到端自检：物理移除推送文件后，应用必须照常启动并服务。

比 tests/test_optional_notify.py 更狠一层：那个是模拟 import 失败，
这个是**真的把文件挪走**，真的起一个进程，真的发 HTTP 请求。

为什么还要这一层
----------------
模拟能证明「import 兜住了」，证明不了「整个应用起得来」。
启动阶段还会做路由索引、权限自检、模板预载等一堆事，
其中任何一处碰到 notify 都会在**服务起不来**的形态下暴露——
那种形态在生产上表现为「容器反复重启」，日志里只有一行 ImportError。

跑完保证还原工作区。挪文件前先确认目标位置空闲。
"""
from __future__ import annotations

import http.cookiejar
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = r"C:\Users\Administrator\.proma\agent-workspaces\default\workspace-files\renovation-pms"
STASH = os.path.join(os.environ["TEMP"], "opencode", "notify-stash")
BASE = "http://127.0.0.1:8018"
PORT = 8018

MOVED = [
    ("app/notify.py", "notify.py"),
    ("scripts/push_alerts.py", "push_alerts.py"),
    ("deploy/push-crontab.txt", "push-crontab.txt"),
]

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (f" -- {detail}" if detail and not cond else ""))


def kill_server():
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen "
         f"-ErrorAction SilentlyContinue) | ForEach-Object "
         f"{{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"],
        capture_output=True)
    time.sleep(2)


def login():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.open(BASE + "/login", timeout=20)
    op.open(BASE + "/login", urllib.parse.urlencode(
        {"username": "admin", "password": "admin123"}).encode(), timeout=20)
    return op


def get(op, path):
    try:
        r = op.open(BASE + path, timeout=25)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return "ERR:%s" % type(e).__name__, ""


def main():
    print("=== 精简部署端到端自检（物理移除 notify.py）===\n")

    # 前置：确保 stash 是空的，否则上次中断会把文件吞掉
    if os.path.isdir(STASH):
        print("[警告] 发现上次残留的 stash，先还原")
        for rel, name in MOVED:
            dst = os.path.join(ROOT, rel.replace("/", os.sep))
            if os.path.exists(os.path.join(STASH, name)):
                shutil.move(os.path.join(STASH, name), dst)
        shutil.rmtree(STASH, ignore_errors=True)

    kill_server()
    os.makedirs(STASH, exist_ok=True)
    moved = []
    for rel, name in MOVED:
        src = os.path.join(ROOT, rel.replace("/", os.sep))
        if os.path.exists(src):
            shutil.move(src, os.path.join(STASH, name))
            moved.append(rel)
    print("已移走：", "、".join(moved) if moved else "（文件本来就不存在）\n")

    proc = None
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        out = open(os.path.join(os.environ["TEMP"], "opencode", "pms_thin.log"), "w",
                   encoding="utf-8")
        err = open(os.path.join(os.environ["TEMP"], "opencode", "pms_thin.err"), "w",
                   encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "run.py", str(PORT)], cwd=ROOT,
                                stdout=out, stderr=err, env=env)

        # 等端口起来
        up = False
        for _ in range(30):
            time.sleep(1)
            if proc.poll() is not None:
                break
            try:
                if urllib.request.urlopen(BASE + "/login", timeout=5).status == 200:
                    up = True
                    break
            except Exception:
                continue

        if proc.poll() is not None:
            check("服务进程未提前退出", False,
                  f"退出码 {proc.returncode}；见 %TEMP%\\opencode\\pms_thin.err")
            return
        check("进程存活", proc.poll() is None)
        check("服务启动成功（精简部署不会因缺 notify.py 而崩）", up)
        if not up:
            return

        op = login()

        # 关键页面逐个访问
        for path, label in (("/oa/settings", "公司设置"),
                            ("/", "首页"),
                            ("/projects", "项目列表"),
                            ("/tasks", "任务页"),
                            ("/todo", "待办总览"),
                            ("/customers", "客户")):
            st, _ = get(op, path)
            check(f"{label} ({path}) 可访问", st == 200, f"实际 {st}")

        # 设置页不应出现推送卡片
        st, html = get(op, "/oa/settings")
        has = ("WECOM_WEBHOOK" in html) or ("测试推送" in html)
        check("设置页不出现推送卡片（没部署就不给一个点了必失败的按钮）",
              st == 200 and not has, f"卡片出现={has}")

        # notify-test 不应 500
        st, _ = get(op, "/oa/notify-test")
        check("notify-test 不 500（未部署时跳转说明）",
              st in (200, 303), f"实际 {st}")

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        kill_server()
        for rel, name in MOVED:
            dst = os.path.join(ROOT, rel.replace("/", os.sep))
            s = os.path.join(STASH, name)
            if os.path.exists(s):
                shutil.move(s, dst)
        shutil.rmtree(STASH, ignore_errors=True)
        print("\n已还原工作区")

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
