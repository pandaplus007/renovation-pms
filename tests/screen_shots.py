"""经营大屏截图：按真实投屏分辨率出图，供肉眼查。

为什么不加进 tests/view_shots.py：那个脚本的 SHOTS 是**工作区**页面，
视口固定 1500x1050 且要过侧栏（clip x=228）。大屏没有侧栏、且是 16:9
固定画布（position:fixed + overflow:hidden），用工作区的裁剪方式截出来
会缺右边一块，看起来像布局塌了。

⚠ 大屏不读 localStorage 的主题（固定深色），所以这里**不设** pms-theme——
   设了也没用，反而会让人误以为它在跟随系统。

三种投屏环境都要看一眼，因为它们暴露的是不同的问题：
  1920x1080  会议室电视 / 桌面显示器（主流）
  1600x900   便宜的一体机、笔记本外接
  1366x768   老电视投屏设备。**这一档最危险**：vh 单位全部缩到 0.71 倍，
             若某处没考虑缩放，这里最先露出「字太小 / 被裁掉」。
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8018"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "shots")

VIEWS = [
    ("screen_1920", 1920, 1080),
    ("screen_1600", 1600, 900),
    ("screen_1366", 1366, 768),
]

os.makedirs(OUT, exist_ok=True)

with sync_playwright() as pw:
    br = pw.chromium.launch()
    ctx = br.new_context()
    pg = ctx.new_page()

    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    for name, w, h in VIEWS:
        pg.set_viewport_size({"width": w, "height": h})
        pg.goto(BASE + "/dashboard/screen")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(300)
        path = os.path.join(OUT, name + ".png")
        pg.screenshot(path=path)
        print("  -> %s  (%dx%d)" % (path, w, h))

    br.close()

print("\n三张截图在 %s" % OUT)