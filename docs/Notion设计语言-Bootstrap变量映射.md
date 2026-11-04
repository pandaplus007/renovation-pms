# Notion 设计语言 · Bootstrap 变量映射

> **结论（2026-09-22）**：本项目**最终未采用** Notion 配色。已按用户要求把全站配色改回原有的「低饱和绿」方案，
> 本项目继续沿用绿色配色。下文映射表保留作为“改版前 → Notion”的对照记录，如需再次尝试 Notion 配色可参考。
>
> **回退方法**：改版前的完整 `app/static/css/style.css` 可从工作区根目录的 `renovation-pms-deploy.tar.gz`（8/24 构建）取回：
> `tar xzf renovation-pms-deploy.tar.gz -O renovation-pms/app/static/css/style.css`
> 注意该包已不包含后端新改动，**只应用于取色/对照，切勿整包覆盖代码**。
> 本次回退只改颜色、保留了后续新增的无色结构（如 `.table-card` 移动端表格卡片化、`.content` 内距）。
> Notion 版 CSS 备份：项目根 `style.notion.css.bak`。

把 `renovation-pms/DESIGN.md`（Notion 设计系统）落到本项目现有 CSS 变量上的对应关系与可直接使用的覆盖块。

- 依据文件：`renovation-pms/DESIGN.md`
- 适配对象：`app/static/css/style.css`（`:root` 变量 + Bootstrap 5.3.3）
- 生成时间：2026-09-22
- **本文件只做映射，未修改任何现有代码。** 是否启用由你决定。

---

## 1. 变量映射表

### 颜色

| 项目变量 | 现值（低饱和绿） | Notion 取值 | 依据 / 说明 |
|---|---|---|---|
| `--primary` | `#3d7a63` | `#0075de` | Notion Blue，主 CTA、链接、交互强调。全站唯一饱和色 |
| `--primary-2` | `#6aa88c` | `#62aef0` | Link Light Blue，深色底上的浅色链接 |
| `--primary-grad` | 绿色渐变 | **取消**，改纯色 `#0075de` | Notion 无渐变主色；DESIGN.md 的 Don't 明确避免装饰性渐变 |
| `--accent` | `#4e8f73` | `#213183` | Deep Navy，次级强调、深色区块 |
| `--success` | `#10b981` | `#1aae39` | Notion Green（确认、完成） |
| `--warning` | `#f59e0b` | `#dd5b00` | Notion Orange（警告、需注意） |
| `--danger` | `#ef4444` | `#d44c47` ⚠️ | **DESIGN.md 未定义危险色**，此为补齐项，见第 3 节 |
| `--bg` | `#f4f6f2` | `#f6f5f4` | Warm White，带黄棕底色的暖白 |
| `--surface` | `#ffffff` | `#ffffff` | 卡片保持纯白 |
| `--surface-2` | `#f7faf8` | `#faf9f8` ⚠️ | 由 Warm White 提亮一档的次级面，为推导值 |
| `--border` | `#e0e9e3` | `rgba(0,0,0,0.1)` | Whisper border，1px 耳语级分隔 |
| `--border-strong` | `#cbd8d0` | `rgba(0,0,0,0.18)` ⚠️ | 提权一档，用于表头、分组边界，为推导值 |
| `--text` | `#1e293b` | `rgba(0,0,0,0.95)` | Notion 主文字，刻意不用纯黑 |
| `--text-2` | `#64748b` | `#615d59` | Warm Gray 500，次级文字 |
| `--text-3` | `#94a3b8` | `#a39e98` | Warm Gray 300，占位与禁用态 |

### 圆角

| 项目变量 | 现值 | Notion 取值 | 用途 |
|---|---|---|---|
| `--radius` | `12px` | `12px`（不变） | 标准卡片，与 Notion 标准卡片一致 |
| `--radius-sm` | `8px` | `8px`（不变） | 小卡片、内联容器 |
| `--radius-xs`（新增） | — | `4px` | 按钮、输入框、下拉 |
| `--radius-list`（新增） | — | `5px` | 列表项、菜单项 |
| `--radius-pill`（新增） | — | `9999px` | 状态徽章、胶囊 |

### 阴影

Notion 的阴影是多层极低不透明度叠加（单层 0.01–0.05），不是单层硬阴影。现值是绿调双层阴影，需整体替换。

| 项目变量 | Notion 处理 |
|---|---|
| `--shadow-sm` | Soft Card 4 层，最大不透明度 0.04 |
| `--shadow` | Soft Card 4 层（略强） |
| `--shadow-lg` | Deep Card 5 层，最大不透明度 0.05、最远 52px 模糊 |

### 侧边栏

| 项目变量 | 现值 | Notion 取值 | 说明 |
|---|---|---|---|
| `--sidebar-bg` | 绿色渐变 | `#f6f5f4`（暖白纯色） | Notion 无渐变侧栏；区块靠底色交替而非描边区分 |
| `--sidebar-text` | `#3a5d52` | `#615d59` | Warm Gray 500 |
| `--sidebar-active` | 绿色渐变 | 底 `rgba(0,0,0,0.05)` + 文字 `#0075de` | 对应 Notion 的次级按钮规范 |

> 这是视觉改动最大的一处。绿色渐变侧栏换成暖白纯色后，整个界面的「清新感」会转向「工具感」，这正是不换 Notion 就得不到的效果。

---

## 2. 可直接粘贴的覆盖块

追加到 `app/static/css/style.css` 的 `:root` 之后即可试色。建议先只改颜色，确认观感后再改圆角与阴影。

```css
/* ============================================================
   Notion 设计语言覆盖层
   依据 renovation-pms/DESIGN.md（来源 vibeui.top）
   覆盖顺序必须在本文件原有 :root 之后
   ============================================================ */
:root {
  /* 主色与语义色 */
  --primary: #0075de;
  --primary-2: #62aef0;
  --primary-grad: #0075de;          /* Notion 无渐变主色 */
  --accent: #213183;
  --success: #1aae39;
  --warning: #dd5b00;
  --danger: #d44c47;                /* DESIGN.md 未定义，补齐项 */

  /* 背景与表面 */
  --bg: #f6f5f4;
  --surface: #ffffff;
  --surface-2: #faf9f8;

  /* 边线：耳语级 1px */
  --border: rgba(0, 0, 0, .10);
  --border-strong: rgba(0, 0, 0, .18);

  /* 文字：不用纯黑 */
  --text: rgba(0, 0, 0, .95);
  --text-2: #615d59;
  --text-3: #a39e98;

  /* 圆角 */
  --radius: 12px;
  --radius-sm: 8px;
  --radius-xs: 4px;
  --radius-list: 5px;
  --radius-pill: 9999px;

  /* 阴影：多层极低不透明度叠加 */
  --shadow-sm:
    rgba(0,0,0,.01) 0 1px 3px,
    rgba(0,0,0,.02) 0 3px 7px;
  --shadow:
    rgba(0,0,0,.01) 0 1px 3px,
    rgba(0,0,0,.02) 0 3px 7px,
    rgba(0,0,0,.02) 0 7px 15px,
    rgba(0,0,0,.04) 0 14px 28px;
  --shadow-lg:
    rgba(0,0,0,.01) 0 1px 3px,
    rgba(0,0,0,.02) 0 3px 7px,
    rgba(0,0,0,.02) 0 7px 15px,
    rgba(0,0,0,.04) 0 14px 28px,
    rgba(0,0,0,.05) 0 23px 52px;

  /* 侧边栏 */
  --sidebar-bg: #f6f5f4;
  --sidebar-text: #615d59;
  --sidebar-active: #0075de;
}

/* 字体：Notion 为 Inter + 中文回退 */
body {
  font-family: 'Inter', -apple-system, system-ui, 'Segoe UI',
    'PingFang SC', 'HarmonyOS Sans SC', 'Microsoft YaHei',
    'Helvetica Neue', Arial, sans-serif;
  font-size: 16px;
}

/* 按钮与输入：Notion 用 4px 小圆角，不是 8/12px */
.btn { border-radius: var(--radius-xs); font-weight: 500; }
.form-control, .form-select { border-radius: var(--radius-xs); border-color: var(--border); }
.btn-primary { background: #0075de; border-color: #0075de; }
.btn-primary:hover, .btn-primary:focus { background: #005bab; border-color: #005bab; }

/* 次级按钮：半透明暖灰底 + 近黑文字 */
.btn-secondary, .btn-outline-secondary {
  background: rgba(0, 0, 0, .05);
  border-color: transparent;
  color: rgba(0, 0, 0, .95);
}
.btn-secondary:hover, .btn-outline-secondary:hover {
  background: rgba(0, 0, 0, .08);
  border-color: transparent;
  color: rgba(0, 0, 0, .95);
}

/* 卡片：细边线 + 柔和多层阴影 */
.card {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
}

/* 表格：金额用表格数字，竖列对齐 */
.table { border-color: var(--border); }
.table th {
  font-weight: 600;
  color: var(--text-2);
  border-bottom-color: var(--border-strong);
}
.table td { border-bottom-color: var(--border); }
.table td.num, .table th.num,
.amount, .money {
  text-align: right;
  font-variant-numeric: tabular-nums;
  font-feature-settings: 'lnum' 1, 'tnum' 1;
}

/* 状态徽章：胶囊 + 浅底 */
.badge { border-radius: var(--radius-pill); font-weight: 600; }
.badge-success { background: rgba(26, 174, 57, .12); color: #108c3d; }
.badge-warning { background: rgba(221, 91, 0, .12); color: #b34a00; }
.badge-danger  { background: rgba(212, 76, 71, .12); color: #b3403c; }

/* 键盘焦点：Notion 要求所有交互元素有 2px 焦点环 */
:focus-visible {
  outline: 2px solid #0075de;
  outline-offset: 2px;
}
```

---

## 3. 需要你确认的三个补齐项

DESIGN.md 没有覆盖到的地方，我按 Notion 官方 UI 的既有做法补了值，但这是**我的推导而非文档原文**，你可以改：

1. **危险色 `#d44c47`** —— DESIGN.md 的语义色只有 Teal / Green / Orange / Pink / Purple / Brown，没有红色。Notion 产品本身用 `#d44c47` 作为红色文字，故采用此值。若你希望严格只用文档内的颜色，替代方案是把「超支/逾期」改用 Orange `#dd5b00`，代价是危险与警告两档会难以区分。
2. **`--surface-2` `#faf9f8`、`--border-strong` `rgba(0,0,0,.18)`** —— 由 Warm White 与 Whisper border 各提一档推导，原文未给出。
3. **徽章的浅底派生色**（如 `rgba(26,174,57,.12)`）—— Notion 的徽章规范只给了蓝色胶囊（底 `#f2f9ff`、字 `#097fe8`），其它语义色胶囊的浅底按同一「12% 透明度」逻辑推导。

---

## 4. 落地建议与风险

**改动顺序**（每步都可独立回滚，建议逐段验证）：

1. 只替换颜色变量 → 看整体气质是否转向「工具感」。
2. 再替换圆角（按钮 12px→4px 是感知最明显的单点变化）。
3. 最后替换阴影与侧边栏底色。

**已知风险**：

- 侧边栏改为暖白纯色后，与白色卡片面的对比会变弱，需要靠 `--border` 的 1px 边线区分层次；现有 `_sidebar.html` 里的绿色系内联样式（如 `.proj-tree` 系列中的 `#48664f`、`#edf1ee`、`#173d24` 等硬编码色值）不会随变量一起变，需要单独清理，否则会出现绿白混搭。
- `base.html` 通过 CDN 引入 Bootstrap，若运行环境不通外网，样式会整体失效——这与本次改动无关，但会干扰你验证效果。
- Inter 字体未内嵌，未安装时会回退到系统中文字体，观感与 Notion 官网略有差异。

**验收方式**：启动 `python run.py`，访问 `http://127.0.0.1:8018`，对比工作台、项目列表、成本明细三个页面的观感一致性。
