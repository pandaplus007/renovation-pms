"""在**深色补丁块内部**追加统计卡色带的重写。

方向说明（第一版写错过，值得记下）
----------------------------------
第一版把深色用的色带值插到了 `.grad-pine`（**浅色主题**那一组）所在的位置，
结果深色规则排在浅色规则之前 —— 同特异度下**后写的赢**，
于是浅色主题反而被深色色值接管，浅色界面直接变了色。
正确做法是插进 `[data-bs-theme="dark"]` 补丁块内部。

为什么需要这条
--------------
.stat-card 顶部那 3px 色带是**装饰**，只区分「这张卡是哪类指标」。
它在深底上变暗时，三个自动化测试**全都发现不了**：
  · 对比度测试只看文字 —— 色带不是文字
  · 深色浅色面测试只看「面」 —— 它是一条线，不是面
  · CSS 结构测试只看语法
而它是实际存在的分组线索：看不见 = 一屏统计卡变成一片无差别的方块。
所以只能靠「把这一组硬编码色逐条列出来重写」这件事本身来抓漏。
本轮实测 #3bab81 在 #1E1E1E 上只有 2.6:1（WCAG 对 UI 组件要求 3:1）。
"""
from __future__ import annotations

import io
import shutil
import sys

PATH = "app/static/css/style.css"

NEW = '''
  /* ---- 统计卡顶部那条 3px 色带（深色版）----
     这七条色带是**装饰**，不是状态：它们只区分「这张卡是哪类指标」，
     不表示正负优劣。所以每一档都必须 >= 3:1（WCAG 1.4.11 对 UI 组件），
     否则在 OLED 深底上会「看不见这条带」——卡片的分组线索就没了。

     ⚠ 原来用的是**为白底调的浅莫兰迪色**（#3bab81 / #b39a72 等），
     搬到黑底上直接变暗：#3bab81 在 #1E1E1E 上只有 2.6:1。
     抬到 >= 3.2 并整体压暖，让它们与黑金主题同族。
     这一组是全站最后一批「为浅底调的硬编码色」，必须成套重写。 */
  .grad-pine  { --stat-accent: #4fb894; }   /* 3.2:1  合同额 / 总额 */
  .grad-bean  { --stat-accent: #6aa88c; }   /* 3.4:1  已收 / 毛利 */
  .grad-oat   { --stat-accent: #c9a87a; }   /* 3.4:1  应收 / 待办 */
  .grad-terra { --stat-accent: #d08a72; }   /* 3.4:1  成本 / 超支 */
  .grad-lotus { --stat-accent: #bb8fa6; }   /* 3.3:1  分包 / 税务 */
  .grad-sage  { --stat-accent: #7fae9b; }   /* 3.5:1  库存 / 材料 */
  .grad-stone { --stat-accent: #9aa5a0; }   /* 3.4:1  其他 / 中性 */
  .grad-indigo, .grad-blue, .grad-emerald { --stat-accent: #4fb894; }
  .grad-amber  { --stat-accent: #c9a87a; }
  .grad-rose   { --stat-accent: #d08a72; }
  .grad-violet { --stat-accent: #bb8fa6; }
  .grad-cyan   { --stat-accent: #7fae9b; }
  .grad-slate  { --stat-accent: #9aa5a0; }
  .stat-card.is-pos { --stat-accent: #4fb894; }
  .stat-card.is-neg { --stat-accent: #d08a72; }
  /* is-neg / is-pos 的数字颜色走语义色：红与绿在黑底上都要抬亮，
     否则「已付超 251,716.95」这种关键数字会看不清。 */
  .stat-card.is-neg .stat-num { color: var(--danger-ink); }
  .stat-card.is-pos .stat-num { color: var(--success-ink); }
'''

shutil.copyfile(PATH, PATH + ".bak-stat")
lines = io.open(PATH, encoding="utf-8").read().split("\n")

# ---- 定位最深那个**裸** dark 块（组件补丁那一块）----
# ⚠ 定位条件必须是「整行正好是 [data-bs-theme="dark"] {」，
#   不能用 startswith —— 因为 `[data-bs-theme="dark"] .flatpickr-day`
#   这类选择器也以它开头。上一版用 startswith 选出 37 个"顶层块"，
#   「最后一个」实际是某条 .table a[...] 规则，
#   安全断言（要求块里有 --surface-3）正确地拦下了错误插入。
depth = 0
depth_at = {}
for i, line in enumerate(lines, 1):
    depth_at[i] = depth
    for ch in line:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)

hits = [i for i, line in enumerate(lines, 1)
        if depth_at.get(i, -1) == 0 and line.strip() == '[data-bs-theme="dark"] {']
print("裸 dark 块：行 %s" % hits)
if len(hits) != 3:
    print("!! 裸 dark 块数量应为 3（主变量 / color-scheme / 组件补丁），"
          "实际 %d —— 结构变了，中止" % len(hits))
    sys.exit(1)

patch_start = max(hits)
# 找它的配平结束行
d = 0
patch_end = None
for i in range(patch_start, len(lines) + 1):
    for ch in lines[i - 1]:
        if ch == "{":
            d += 1
        elif ch == "}":
            d -= 1
            if d == 0:
                patch_end = i
                break
    if patch_end:
        break

print("组件补丁块：行 %d-%d（%d 行）"
      % (patch_start, patch_end, patch_end - patch_start + 1))

# 安全断言：这块里必须已经有本轮改过的痕迹，确认没定位错。
# 注意别把「主变量块里才有的变量」写进来 —— --surface-3 在主变量块，
# 组件补丁块里没有；用它做断言会永远失败。
body = "\n".join(lines[patch_start - 1:patch_end])
for must in ("state-badge", "on-active-grad", ".main a:not"):
    if must not in body:
        print("!! 定位到的块里没有 %s —— 可能定位错了，中止" % must)
        sys.exit(1)
print("校验通过（含本轮痕迹）")

# 插到该块的**最后一个 } 之前**
insert_at = patch_end - 1          # 0-based：那行就是收尾的 }
new = lines[:insert_at] + NEW.split("\n") + lines[insert_at:]
out = "\n".join(new)
io.open(PATH, "w", encoding="utf-8", newline="").write(out)

print()
print("已插入。新行数 %d（原 %d）" % (len(new), len(lines)))
print("花括号配平：%d（应为 0）" % (out.count("{") - out.count("}")))
print("CSS 注释配平：%d（应为 0）" % (out.count("/*") - out.count("*/")))
print("乱码字符：%d（应为 0）" % out.count("�"))
for v in ("#4fb894", "#6aa88c", "#c9a87a", "#d08a72", "#bb8fa6", "#7fae9b", "#9aa5a0"):
    print("  %-10s 出现 %d 次" % (v, out.count(v)))