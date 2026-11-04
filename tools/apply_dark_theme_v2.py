"""按行范围替换深色主题的变量块。

为什么用脚本而不是 edit 工具
----------------------------
这个块 150 行、含大量中文注释与全角标点，edit 的精确字符串匹配
在这么长的文本上极易因为一个看不见的差异而失败（试过一次）。
按「起止行号 + 内容校验」替换更稳：先验证定位到的确实是
那个块（首行是 [data-bs-theme="dark"] {、末行是单独一个 }），
再替换，并在替换后回读校验行数与花括号配平。
"""
from __future__ import annotations

import io
import shutil
import sys

PATH = "app/static/css/style.css"
NEW_BLOCK = r'''/* ============================================================================
   深色主题 v2：炭灰三层 + 金色强调 + 四色语义
   ----------------------------------------------------------------------------
   值全部由 tools/build_dark_theme.py 反解产出，**别手改**。
   改法：改那个脚本里的目标值，重跑，把输出粘回来。

   ---------- v1 -> v2 改了什么，为什么 ----------
   v1 是「近黑底 #0a0a0a + 满屏金」，实际用起来两个问题：
     1. 底太死。#0a0a0a 亮度 0.0030，接近纯黑，卡片只能靠极小的
        明度差分层，在 OLED 上等于没有边界。
     2. 金色覆盖七处（链接 / 侧栏图标 / 状态徽章 / 进度条 / 主按钮 /
        卡片标题 / 单号底色），满屏金 = 把「黄金」当「油漆」用了。
   v2 的方向：
     · 底色抬到 #121212，并明确三层抬升 #121212 / #1E1E1E / #2D2D2D
     · 卡片额外加 1px rgba(255,255,255,.08) 描边 —— **明度差 + 描边双保险**，
       只靠色差在 OLED 上会糊
     · 金色只承担四件事：链接、主按钮、进度条、金额强调
     · 状态不再靠金色区分，改为四色语义（绿 / 橙 / 红 / 蓝）

   ---------- 为什么不能沿用 v1 的明度参数 ----------
   WCAG 亮度权重 R 0.2126 / G 0.7152 / B 0.0722，绿光权重最高、蓝色最低，
   **同样的 HSL 明度，绿色看起来比蓝色亮得多**。更关键的是这一轮底色
   从 #0a0a0a（亮度 0.0030）抬到 #121212（亮度 0.0060），同样的前景色
   对比度会**下降** —— v1 的数字不能跨底色搬运，必须重算。
   ============================================================================ */
[data-bs-theme="dark"] {
  /* ---------- 中性色阶 ----------
     surfaces（50~300）饱和度取 0：那是面积最大的地方，一点偏色就被读成
     「底色脏」。文字档（400~700）取 0.02~0.04 的**暖中性**，与金色同色温；
     800 / 900 是用户直接给定的值，不反解。 */
  --gray-50:#121212;
  --gray-100:#181818;
  --gray-200:#2c2c2c;
  --gray-300:#404040;
  --gray-400:#5d5c59;
  --gray-500:#777570;
  --gray-600:#838079;
  --gray-700:#a8a6a1;
  --gray-800:#CBD5E1;
  --gray-900:#F1F5F9;

  /* rgb 三元组必须跟着改：Bootstrap 的 -rgb 变量是写死的十进制数，
     换色阶而忘了它，hover / 阴影 / 条纹会继续用旧色调。
     这几个都是从上面色阶里取同一支颜色反算的：
       gray-900 #F1F5F9 -> 241,245,249
       gray-600 #838079 -> 131,128,121
       gray-500 #777570 -> 119,117,112
       gray-200 #2c2c2c ->  44, 44, 44
       gold     #c9a227 -> 201,162, 39 */
  --bs-emphasis-color-rgb: 241, 245, 249;
  --bs-secondary-color-rgb: 131, 128, 121;
  --bs-secondary-rgb: 131, 128, 121;
  --bs-secondary-bg-rgb: 44, 44, 44;
  --bs-tertiary-color-rgb: 119, 117, 112;

  /* 链接**保留金色**（用户明确要留）—— 项目名、单号、合同号都是链接，
     这是金色唯一大面积出现的地方，所以它必须始终可读：
       大背景 7.74:1 / 卡片 6.89:1 / 悬停底 5.69:1 / 徽章底 5.68:1
     hover 用提亮档不用反色 —— 反色在深底上等于「变暗」，更看不清。

     ⚠ 表格行 hover 改成**中性**白 3.5%：行悬停不是链接，
       不该也染上金色 —— 这是 v1「金色当油漆用」最明显的一处。 */
  --bs-link-color: #c9a227;
  --bs-link-hover-color: #e0b93a;
  --bs-link-color-rgb: 201, 162, 39;
  --bs-link-hover-color-rgb: 224, 185, 58;
  --bs-table-hover-bg: rgba(255, 255, 255, .035);

  /* ---------- 表面三层：抬升感 ----------
     只靠明度差不够：#121212 与 #1E1E1E 的对比只有 1.12:1，
     OLED 屏上几乎看不出边界。所以卡片再加 1px 半透明白描边。 */
  --surface: #1E1E1E;
  --surface-2: var(--gray-100);
  --surface-3: #2D2D2D;          /* 悬停态 / 下拉菜单 / 弹出层 */
  --card-border: rgba(255, 255, 255, .08);

  /* 输入框要比背景**更暗**才有「凹陷、可以往里填」的感觉。
     比背景亮的话，用户会以为那是一块可以点的区域。 */
  --input-bg: #0E0E0E;
  --input-border: #404040;

  /* ---------- 品牌色 ----------
     深色主题用古铜金 #c9a227，与炭灰底同色温，不产生色彩振动。
     浅色主题仍是绿色品牌 —— 深浅两套独立推导，
     不能靠「把颜色反过来」了事。 */
  --primary-ink: #c9a227;
  --accent: #c9a227;

  /* 主按钮 / tab 选中 / 进度条共用这条渐变。深色下也是金色，
     因为它属于「操作 + 金额」这一类，是金色该待的地方。
     两端白字 6.20:1 与 4.80:1 —— 金色亮度必须 <= 0.1833 才压得住白字，
     所以别为了「更亮一点」去提它。 */
  --primary-grad: linear-gradient(135deg, #7a5c1a 0%, #8a6f22 100%);
  --primary-grad-hover: linear-gradient(135deg, #8a6f22 0%, #8f7426 100%);

  /* ---------- 四色语义 ----------
     用户要求四色全上：成功绿 / 警告橙 / 错误红 / 信息蓝。
     每个色都同时对**大背景**与**卡片底**验算过 ——
     两层底差半个档，只验一层会漏（danger 就是这么漏的：
     #EF4444 对大背景 4.98:1 过，对卡片底只有 4.43:1）。 */
  --success-ink: #22C55E;   /* 8.22:1 / 7.32:1 */
  --warning-ink: #F97316;   /* 6.68:1 / 5.95:1 */
  --danger-ink:  #d9655d;   /* 5.31:1 / 4.74:1  <- 原定 #EF4444 卡片底不够，抬亮 */
  --info-ink:    #3B82F6;   /* 5.09:1 / 4.53:1 */

  /* 徽章底由脚本反解（每种色的暗底上限不同，写死一套会让某一档压线）。
     浅底在深色主题里会像一排小灯泡，所以一律「暗底 + 亮字」。 */
  --success-bg: #223f30;
  --warning-bg: #372a1e;
  --danger-bg:  #2a1816;
  --info-bg:    #121822;

  /* ---------- 大面积金色 ----------
     选中态文字有两种走法，这一版**第一次两者都成立**：
       正文色 #F1F5F9 压 --gold-deep -> 4.85:1  ✓
       纯白     #ffffff  压 --gold-deep -> 5.32:1  ✓（更保险）
     v1 时这两条是互斥的（金块要显眼 >=3:1 与 正文色压上去够清楚 >=4.5:1
     在亮度上无交集），只能退回纯白。抬底之后正文色也可用了，
     但 --on-gold 仍保留：换更浅的金色时它是唯一解。 */
  --gold-deep: #7f6925;
  --gold-bar:  #997c26;
  /* 两个「压在金色上的文字色」，别混：
       --on-gold        压 --gold-deep（选中行、侧栏高亮）-> 白字 5.32:1
       --on-gold-bright 压满格金 / --primary-grad（主按钮）   -> 近黑字 6.20:1
     一个金底配白字还是黑字，取决于底有多亮，不是随意的。 */
  --on-gold:        #ffffff;
  --on-gold-bright: #0a0a0a;

  /* 语义描边按钮：亮色 + **近黑**文字。别写白字 ——
     白字压 #22C55E 只有 1.4:1。 */
  --success-btn: var(--success-ink);
  --on-success-btn: #0a0a0a;
  --danger-btn: var(--danger-ink);
  --on-danger-btn: #0a0a0a;

  /* 项目详情页横幅：同属卡片表面族，比卡片略亮一档，
     用暖灰色相而不是亮度做区分。 */
  --proj-hero-bg: linear-gradient(135deg, #242424 0%, #1d1d1d 60%, #191919 100%);

  /* 深色下的表头：比卡片略暗一档。表头本该是这一块的标题，
     理应更突出 —— 但「更突出」不等于「更亮」：一屏十几张表头都更亮，
     反而比没有层级更乱。 */
  --head-bg: var(--gray-100);
  --head-fg: var(--gray-900);
  --zebra: rgba(255, 255, 255, .028);

  /* 深色下的卡片标题栏：底比卡片**暗**一档而不是亮（同上）。
     标题文字用正文色，不用金色 —— 金色留给链接与金额。 */
  --primary-text: #c9a227;
  --card-head-bg: linear-gradient(90deg, #1a1a1a, #161616);
  --card-head-fg: var(--gray-900);
  --card-head-border: var(--card-border);
  --card-head-muted: var(--gray-600);
  --card-head-solid: #1a1a1a;
  --card-head-solid-border: var(--card-border);
  /* 顶栏用半透明黑：半透明白会在深色界面顶部横一条亮带。
     底色跟着 --gray-50 走，换底色时这里要一起改。 */
  --topbar-bg: rgba(18, 18, 18, .82);

  /* 深色主题里阴影几乎不可见（黑上加黑），改用「描边 + 更深的暗部」
     来制造层次 —— 这也是 Linear 深色模式的实际做法。 */
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, .35);
  --shadow: 0 2px 8px rgba(0, 0, 0, .42);
  --shadow-lg: 0 12px 32px rgba(0, 0, 0, .55);

  /* 侧边栏：与卡片同属中间层（用户给的 #1E1E1E），
     稍微压深一档让内容区更突出。文字用用户给的 #A1A1AA（6.50:1）。
     未选中项是灰的，**图标也一样是灰的** —— v1 把链接色套在
     a:not(.btn) 上，导致整条侧栏的图标和文字全金，这是本轮要修的头号问题。 */
  --sidebar-bg: linear-gradient(180deg, #1b1b1b 0%, #171717 55%, #141414 100%);
  --sidebar-text: #A1A1AA;
  --sidebar-active: linear-gradient(90deg, #5c4a1c, #7f6925);
  /* 分组标题比导航项更弱一级但仍要可读，用 --gray-600（4.63:1）。
     「未分配项目」是**警示**语义，走橙，不用金色。 */
  --sidebar-group-fg: var(--gray-600);
  --unassigned-fg: var(--warning-ink);
}
'''

shutil.copyfile(PATH, PATH + ".bak-v2")
print("已备份 -> %s.bak-v2" % PATH)

lines = io.open(PATH, encoding="utf-8").read().split("\n")

# 定位：第一个顶层 [data-bs-theme="dark"] { 的块（用 tools/css_structure.py 同款判据：
# 该行花括号深度为 0，即顶层，不是 @media print 里的那个）
depth = 0
depth_at = {}
for i, line in enumerate(lines, 1):
    depth_at[i] = depth
    for ch in line:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)

start = None
for i, line in enumerate(lines, 1):
    if depth_at.get(i, -1) == 0 and line.strip().startswith('[data-bs-theme="dark"]'):
        start = i
        break
if start is None:
    print("!! 找不到顶层深色块")
    sys.exit(1)

# 找它的配平结束行
d = 0
end = None
for i in range(start, len(lines) + 1):
    for ch in lines[i - 1]:
        if ch == "{":
            d += 1
        elif ch == "}":
            d -= 1
            if d == 0:
                end = i
                break
    if end:
        break

print("定位到顶层深色块：行 %d - %d（%d 行）" % (start, end, end - start + 1))
print("  首行：%s" % lines[start - 1].strip()[:60])
print("  末行：%s" % lines[end - 1].strip()[:60])

body = "\n".join(lines[start - 1 : end])

# 安全断言：确认就是这个块。
# 只校验**旧块里本来就有**的东西 —— 别拿新块的内容来校验旧块
# （我第一版把 --primary-grad 也列进来，结果它当时在组件补丁块里，
#  不在主变量块，断言误报「定位错了」，白跑一趟）。
for must in ("--gray-50", "--sidebar-bg", "--sidebar-active"):
    if must not in body:
        print("!! 定位到的块里没有 %s，可能定位错了，中止" % must)
        sys.exit(1)

new = lines[: start - 1] + NEW_BLOCK.rstrip("\n").split("\n") + lines[end:]
out = "\n".join(new)
io.open(PATH, "w", encoding="utf-8", newline="").write(out)

print()
print("替换完成。新行数 %d（原 %d）" % (len(new), len(lines)))
print("花括号配平：%d（应为 0）" % (out.count("{") - out.count("}")))
print("CSS 注释配平：%d（应为 0）" % (out.count("/*") - out.count("*/")))
print("乱码字符：%d（应为 0）" % out.count("�"))

# 回读校验：关键值必须在
for v, want in [("--gray-50:#121212", 1), ("--surface: #1E1E1E", 1),
                ("--card-border: rgba(255, 255, 255, .08)", 1),
                ("--info-ink:    #3B82F6", 1),
                ("--sidebar-text: #A1A1AA", 1)]:
    n = out.count(v)
    print("  %-46s %s" % (v, "OK" if n == want else "!! 出现 %d 次" % n))