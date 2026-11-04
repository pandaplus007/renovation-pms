"""按主题批量截图，用于人工核对配色改动。

为什么要单独一个脚本
------------------
1. 每次都要跑「登录 → 切主题 → 逐页截图」，内联在 shell 里既长又容易写错。
2. **必须清 cookie**：登录成功后 `/login` 会直接跳走，
   第二轮就卡在等一个不存在的表单上（Page.fill timeout 30s）。
   这个坑我踩了两次，所以写进脚本里固定下来。
3. 对比度测试已经能自动判定「看不看得清」，但**审美判断做不了自动化**：
   黑金主题到底好不好看、莫兰迪色块压在一起挤不挤，只能人眼看。
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8018"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "shots")

SHOTS = [
    ("home", "/", {"x": 228, "y": 0, "width": 1272, "height": 1040}),
    ("procurement", "/procurement", {"x": 228, "y": 0, "width": 1272, "height": 1000}),
    ("project_detail", "/projects/1", {"x": 228, "y": 0, "width": 1272, "height": 1040}),
    ("payables", "/payables", {"x": 228, "y": 0, "width": 1272, "height": 1040}),
]

themes = sys.argv[1:] or ["light", "dark"]

with sync_playwright() as pw:
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": 1500, "height": 1050})
    pg = ctx.new_page()
    for th in themes:
        ctx.clear_cookies()          # 见文件头第 2 点
        pg.goto(BASE + "/login")
        pg.fill("input[name=username]", "admin")
        pg.fill("input[name=password]", "admin123")
        pg.click("button[type=submit]")
        pg.wait_for_load_state("networkidle")
        pg.evaluate("t => localStorage.setItem('pms-theme', t)", th)
        for name, path, clip in SHOTS:
            pg.goto(BASE + path)
            pg.wait_for_load_state("networkidle")
            pg.wait_for_timeout(350)
            p = os.path.join(OUT, "shot_%s_%s.png" % (th, name))
            pg.screenshot(path=p, clip=clip)
            print("  %s  %s" % (th, name))
    br.close()
print("截图完成 -> tests/shots/shot_<主题>_<页面>.png")