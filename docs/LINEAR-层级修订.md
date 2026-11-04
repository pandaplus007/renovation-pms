# Linear 层级修订层（linear.css）

> 2026-10-18 加的第二层视觉修订。只借 Linear 的**层级纪律**；纸底从 Basecamp 的米白改成蓝色系，主色仍是 `#3568BE`。

## 为什么是"第二层"

Basecamp 层解决的是"气质"（纸白、圆角、柔性阴影、色块），这一层解决的是
"信息密度与层级预算"：Linear 让 3000 个数字挤在一屏还不乱，靠的不是配色，
而是四件很克制的事：

1. 表面只差 3~5 个明度点，不靠阴影分层；
2. 1px 发丝线替代边框，表格行高 36~38px；
3. 文字四级，对比度分别是 12.4 / 6.4 / 5.1 / 2.6:1，最低一级只做装饰；
4. 全屏只有一个实心强调色，语义色退成 10% 透明底的小胶囊。

## 文件

| 文件 | 改动 |
|------|------|
| `app/static/css/linear.css` | **新增**，全部修订规则（约 380 行） |
| `app/templates/base.html` | 在 basecamp.css 之后加一行 `<link ... linear.css>` |
| `app/templates/login.html` | 同上 |
| `app/templates/_sidebar.html` | 分组外包一层 `.nav-group`（分组卡） |
| `app/templates/screen.html` | 同上（排在 screen.css **之前**） |
| `app/templates/dashboard.html` | 项目色块由 `style="background:X"` 改成 `style="--sw:X"` |
| `app/templates/projects/detail.html` | 同上 |
| `app/templates/oa/settings.html` | 「界面外观」三选一（蓝底 / 冷纸白 / 深色） |
| `app/web.py` | `_css_version()` 的文件清单补上 basecamp.css 与 linear.css |

## 外观三档（2026-10-18 晚）

设置页 →「界面外观」，三选一，立即生效、不刷新：

| 选项 | 触发方式 | 画布 / 面板 / 侧栏 |
|------|----------|--------------------|
| 浅色 · 蓝底（默认） | 无属性 | `#EDF3FB` / `#E3ECF8` / `#E5EEF9` |
| 浅色 · 冷纸白 | `<html data-canvas="paper">` | `#F4F7FC` / `#EDF2F9` / `#EDF2F9` |
| 深色 | `data-bs-theme="dark"` | `#101419` / `#1E242C` / `#141920` |

- 存 `localStorage`：`pms-theme`（light/dark）+ `pms-canvas`（blue/paper），
  和右上角月亮按钮共用同一份设置；**只对本设备生效**（没有用户偏好表）。
- 选深色时**不写** `pms-canvas`：切回浅色还是原来那档。
- `linear.css` 里用 `html[data-canvas="paper"]:root:not([data-bs-theme="dark"])`
  覆盖令牌（特异度 0,3,1，压过蓝底那组 0,2,1）；深色下这套自动失效。
- 登录页只有浅色，但会跟着 `pms-canvas` 走。
- 对比度：两档各跑一遍 `test_contrast_wcag.py`（14 页 × 明暗，3310 样本），**0 处不达标**。

### ⚠ 顺带修的缓存坑

`web.py:_css_version()` 原来只把 `style.css / screen.css / 两个 js` 算进版本号，
**basecamp.css 与 linear.css 不在清单里**——改主题层不会改 `?v=`，
浏览器缓存就一直是旧样式（症状：改完刷新没变化，但换无痕窗口就正常）。
已补进清单；以后再加静态文件，记得同样加进去。

## 回滚

删掉 base.html 里的这一行即可（色块的 `--sw` 写法不依赖本文件，留着也无害）：

```html
<link rel="stylesheet" href="/static/css/linear.css?v={{ CSS_VERSION }}">
```

## 令牌变化（浅色 / 深色）

| 令牌 | 旧值（Basecamp） | 新值 | 原因 |
|------|------|------|------|
| `--bg` | `#FEFAF6` 米白 | `#EDF3FB` 蓝纸底 | 用户要求换蓝色系 |
| `--surface-2` | `#F8F4F0` | `#E3ECF8` | 面板 / 表头，比画布深一档 |
| `--sidebar-bg` | `#F8F4F0` | `#E5EEF9` | 侧栏比画布深半档，靠 1px 线分界 |
| `--text` | `#2E373D` | `#22303E` | 冷调近黑，压蓝底不脏 |
| `--text-2` | `#6B7177` | `#4E5B69` | 6.4:1 |
| `--text-3` | `#9B9FA2` (2.6:1) | `#5F6B78` (5.1:1) | 旧值连 12px 元信息都不达标 |
| `--border` | `#E9E7E3` | `rgba(20,44,78,.11)` | 冷墨色发丝线 |
| `--line-1/2` | — | `.09 / .16` 冷墨 | 表格分隔线 / 表头线 |
| `--shadow-sm` | 多层柔光 | `0 1px 2px rgba(18,24,30,.05)` | 只保留贴纸面的一层 |
| `--radius-ctl` | — | `8px` | 控件 8、卡片 12，圆角分档 |

深色同步换成冷灰蓝（画布 `#101419` / 面板 `#171C23` / 正文 `#E8EEF6`），
否则浅色冷了、深色还是暖炭，两套主题不像一家人。

## 组件口径

- **表格**：`13px`，行高 38px（`padding: 9px 14px`），表头 11.5px / `--text-2`，
  合计行只加一条 `--line-2` 顶线；数字列 `tabular-nums` 右对齐。
- **卡片**：1px 发丝线 + 微投影，hover 只加深描边，**不再抬升**。
- **按钮**：高 32 / 小号 28，圆角 8，主按钮统一 `--primary`，hover 压深到 `#2F5DAE`。
- **标签**：`6px` 圆角，浅色下 10% 透明底 + 深字；项目首字色块由实色块
  改成「22% 色底 + 正文色字」（原来白字压 `#1DB847` 只有 2.6:1）。
- **侧栏分组卡**：`_sidebar.html` 把每个分组（经营 / 项目 / 采招 / 财务…）
  连同它的子项包进 `.nav-group`——1px `rgba(20,44,78,.07)` 边框、10px 圆角、
  22% 白底（深色 `.03`），组名做成卡头（下方 1px 分隔线）。
  「财务」下面的开票申请 / 发票管理 / 供应商付款 / 应付账款 因此落在同一张卡里。
  条目本身不再单独描边，选中项仍是白底 + 1px 线 + 1px 投影。
- **动效**：统一 `160ms cubic-bezier(.2,.6,.2,1)`。

## 顺手修掉的回归

`basecamp.css` 里 `.main { margin-left: 252px }` 没有媒体查询，而它排在
`style.css` 之后，把 `style.css` 的 `@media (max-width:991.98px){.main{margin-left:0}}`
盖掉了 —— 手机上正文被挤成 138px 宽的一条。本文件按同断点补回，
并加了 575px 以下的卡片/表格内边距收口。

## 验证

- `tests/test_contrast_wcag.py`（14 页 × 明暗两套，3310 个文本样本）：**0 处不达标**。
- `tests/test_all_routes.py`：**81 条 GET 路由全部 200**。
- 截图：`shots/lin-before-*.png` / `shots/lin-after-*.png`（1440×1000 与 390×844）。

## 没抄的部分

纯黑背景、紫色强调色、负字距（中文会粘字）、Linear 官网的大留白节奏。
