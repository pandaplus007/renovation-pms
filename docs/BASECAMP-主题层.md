# Basecamp 主题层（basecamp.css）

> 2026-10-17 加的视觉层。只改外观，不改数据结构与业务逻辑。

## 文件

| 文件 | 改动 |
|------|------|
| `app/static/css/basecamp.css` | **新增**，全部主题规则都在这里（约 1000 行） |
| `app/templates/base.html` | 加一行 `<link ... basecamp.css>`（在 style.css 之后） |
| `app/templates/login.html` | 同上 |
| `app/templates/screen.html` | 同上（排在 screen.css **之前**） |
| `app/templates/dashboard.html` | 首页顶部加了一个问候区（`bc-hero`），纯增量 |

## 回滚

删掉上面三个模板里的这一行即可，不需要动 CSS：

```html
<link rel="stylesheet" href="/static/css/basecamp.css?v={{ CSS_VERSION }}">
```

## 怎么改色

只改 `basecamp.css` 顶部的两个令牌块（`html:root:not([data-bs-theme="dark"])`
与 `html:root[data-bs-theme="dark"]`），不要在下面的组件规则里散写十六进制。
改完 CSS_VERSION 会随文件 mtime 自动变，浏览器不用手动清缓存。

## ⚠ 特异度：这层为什么要用 `html:root...`

`style.css` 末尾本来就有一层 Basecamp 令牌（`:root:not([data-bs-theme="dark"])`，
特异度 0,2,0）；它的深色段还有嵌套的组件规则（`[data-bs-theme="dark"] .xxx`，
编译后 0,3,0）。

所以本文件的令牌块用 `html:root:not(...)`（0,2,1）压过前者，
深色组件修正用 `html[data-bs-theme="dark"] .xxx`（0,3,1）压过后者。
**改动这些选择器前先算一遍特异度**，否则会出现「令牌生效了、某个组件没跟上」
的半套状态（典型症状：深色侧栏的选中项文字变成深色压在深色药丸上）。

## 设计口径（2026-10-17 第二版：改用 Basecamp 原色）

色值直接取自 Basecamp 5 的产品截图采样：

| 用途 | 色值 | 说明 |
|------|------|------|
| 画布 | `#FEFAF6` | 纸白，不是米黄 |
| 面板 / 侧栏 | `#F8F4F0` | |
| 卡片 | `#FFFFFF` | 1px `#E9E7E3` 描边 + 极淡投影 |
| 正文 | `#2E373D` | 11.7:1 |
| 次要文字 | `#6B7177` | 4.75:1 |
| 链接 | `#3568BE` | 文字用（5.2:1）；图标/进度条用原色 `#3D75CB` |
| 强调红 | `#EB455A` | 徽章底色；文字压深为 `#C8354B` |
| 浅蓝 chip | `#D2E8FD` + 字 `#2A5DA8` | 对应它的 All-access 标签 |
| 奶油黄 chip | `#FCF0DB` + 字 `#8A5A06` | 对应它的待办/提醒标签 |
| 彩色圆点 | `#117AF3` `#1DB847` `#FF801F` `#FA423C` `#7A70FF` `#EDB300` | 首页六张指标卡轮转 |

两条蓝是有意分开的：Basecamp 的 `#3D75CB` 压纸白只有 4.39:1，当正文链接差一点，
所以文字压深到 `#3568BE`，装饰性元素保留原色。

所有文字与底色都按 WCAG AA 核算过；新增颜色请沿用这个口径。

## Basecamp 的三个标志性细节

1. **六层柔性阴影** —— `--shadow-sm / --shadow / --shadow-lg` 改成多层叠加、
   用 `em` 单位（随字号缩放），卡片靠阴影浮起而不是靠深边框。
2. **项目封面色块 + 星标** —— `.proj-swatch` 是 26px 圆角色块，
   颜色由 `项目id % 6` 从六色里取，同一项目永远同色；首字作"封面字"。
   星标 `.proj-star` 的状态存在 `localStorage` 的 `pms-starred-projects` 里，
   **只在本浏览器有效**（没做后端收藏表，为一个收藏功能加表+迁移不划算）。
   接入位置：`dashboard.html` 的最近项目表、`projects/detail.html` 的标题旁。
3. **标题紧字距** —— 全局标题 `letter-spacing: -.02em`，首页大标题 `-.03em`。
   中文可挤的余量比拉丁小，再紧就会粘字。

顺带的结构微调：首页"最近项目"表把「截止日期」列收进了项目名的第二行
（`截止 2026-12-01`），否则加了色块和星标以后 5 列在 1440 宽度下会溢出。
表格头从 5 列变 4 列，`test_recv_runtime` 读的「回款」列没动。

