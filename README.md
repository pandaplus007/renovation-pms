# 装修项目管家（Renovation PMS）

面向装修单位 / 个人工作室的项目管理软件：**客户管理、项目总览、成本管理、材料管理、采购库存**一站式。

纯 Python 全栈（FastAPI + SQLAlchemy + Jinja2 + Bootstrap），SQLite 单机运行，浏览器访问即可，无需 Node / 数据库安装。

## 功能一览

| 模块 | 说明 |
|------|------|
| 项目总览 | 在建/待签约统计、合同额与应收余额、收款/工期/成本超支/低库存预警、最近项目 |
| 客户管理 | 客户档案、跟进记录（含下次跟进提醒）、状态流转（线索→已签约）、一键转项目 |
| 项目管理 | 项目档案、施工阶段推进（自动算进度）、收款计划与收款登记、预算 vs 成本、**财务总览（合同/应收/成本 + 已开发票/已收发票/应付供应商/供应商欠款）**、任务清单、现场记录（支持上传照片）、文档（支持上传附件） |
| 成本管理 | 预算科目（**行内编辑**、**Excel 导入**、模板下载）、实际成本登记、项目毛利与毛利率、成本结构报表、导出 Excel |
| 发票管理 | 进项/销项发票登记（类型/税额）、**PDF 上传自动识别**（号码/金额/日期/**购方销方名称+税号单独识别** + 自动匹配客户/供应商/项目）、关联供应商/客户/项目/采购单、汇总、导出 Excel |
| 供应商付款 | 付款记录（方式/日期/关联采购单）、**银行转账单导入存档+自动识别**（图片视觉 / PDF 文本 → 收款方/金额/日期/摘要 → 匹配供应商）、供应商对账（应付=采购单 vs 已付 vs 欠款）、导出对账 |
| 采购（合同+采购单） | **合并页面双 Tab**：采购合同（登记/识别/存档/节点/清单[材料可设供应商与项目]/执行进度/**送货单[未送货红色标识]**）+ 采购单（从合同清单批量添加自动带出供应商项目） |
| 甘特图 | **时间线 / 甘特图双模式切换**：时间线（任务+合同节点按日期垂直排列、今天分隔线）、甘特图（任务时间条 + 合同节点、类型配色、今天线） |
| 材料管理 | 材料库（**材料可设所属项目**）、项目材料清单（自动计算缺口）、预算 vs 采购对比、**Excel 导入预算材料清单（项目页可直达）**、**勾选材料导出采购询价单（含供应商报价空列）**、低库存预警 |
| 库存管理 | 出入库登记、库存总览、低库存预警、流水明细 |
| 供应商 | 供应商档案、关联材料与采购单 |
| 提醒通知 | 顶部铃铛实时提醒：收款到期、工期临近/超期、库存不足 |
| 移动端 | 响应式布局，手机/平板浏览器直接访问，支持部署到 NAS（极空间 Z4 Pro） |

## 快速开始

```bash
# 1. 安装依赖（建议虚拟环境）
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate  # macOS / Linux

pip install -r requirements.txt

# 2. 启动（首次启动自动建表并写入演示数据）
python run.py
# 或指定端口：python run.py 8080

# 3. 浏览器访问
#    http://127.0.0.1:8018
#    默认账号：admin / admin123（登录后可自行修改）
```

## 目录结构

```
renovation-pms/
├── run.py                # 启动入口
├── requirements.txt
├── data/                 # SQLite 数据库（首次启动生成）
└── app/
    ├── main.py           # FastAPI 应用与登录
    ├── models.py         # ORM 数据模型（全部核心表）
    ├── seed.py           # 演示数据
    ├── auth.py           # 会话与密码
    ├── web.py            # 模板渲染设施
    ├── routers/          # 业务路由（dashboard/customers/projects/...）
    ├── templates/        # Jinja2 页面模板
    └── static/           # CSS 样式
```

## 数据模型核心表

`users` 用户 · `customers` 客户 · `follow_ups` 跟进 · `projects` 项目 · `contracts` 合同
`payments` 收款计划 · `budget_items` 预算 · `cost_items` 成本 · `materials` 材料库
`project_materials` 项目材料 · `suppliers` 供应商 · `purchase_orders` 采购单
`inventory_records` 库存流水 · `phases/project_phases` 阶段模板与项目阶段
`tasks` 任务 · `site_records` 现场记录 · `documents` 文档
`invoices` 发票 · `supplier_payments` 供应商付款 · `purchase_contracts` 采购合同 · `contract_materials` 合同采购清单 · `contract_milestones` 合同时间节点 · `gantt_tasks` 甘特图任务 · `uploads/` 上传文件（site 照片 / doc 附件 / invoice 发票 PDF）

## 部署：极空间 Z4 Pro（Docker）

极空间 Z4 Pro 支持 Docker。两种方式任选：

### 方式一：docker compose（推荐）

1. 将整个 `renovation-pms` 文件夹拷贝到极空间的共享目录（如 `docker/renovation-pms`）
2. 通过极空间 SSH/终端进入该目录，执行：
   ```bash
   docker compose up -d --build
   ```
3. 浏览器访问：`http://极空间IP:8018`（手机连接同一局域网同样可访问）
4. 首次使用在项目目录的 `.env` 里设置 `SECRET_KEY` 为随机长字符串（`python -c "import secrets;print(secrets.token_hex(32))"`），compose 会自动注入

### 方式二：极空间 Docker 管理界面

1. 项目文件上传到极空间后，在 Docker 管理界面新建容器，选择从 Dockerfile 构建（指向项目目录）
2. 端口映射：`8018 → 8018`
3. 挂载数据卷（必填，否则容器重建丢数据）：
   - `/app/data` → 极空间目录（SQLite 数据库）
   - `/app/uploads` → 极空间目录（上传的照片/文件）
4. 环境变量：`SECRET_KEY=随机字符串`、`HOST=0.0.0.0`

### 数据备份

- 数据库：`data/renovation.db`（停止容器后直接复制即可）
- 上传文件：`uploads/` 目录
- 建议在极空间上配置定时快照/备份任务

## 安全提示

- 首次登录后请尽快修改默认密码（数据库 data/renovation.db 中 users 表）
- `SECRET_KEY` 为会话签名密钥，生产环境请通过 `.env` 或环境变量替换为随机长字符串（不要提交真实值）
- 数据库文件 `data/renovation.db` 建议定期备份（关闭程序后直接复制文件即可）

## 后续规划（可选扩展）

- 多用户角色权限细分
- 微信小程序端
- 邮件/企业微信提醒推送
## 开源协议

本项目采用 [Apache License 2.0](LICENSE) 开源：可商用、可修改、可再发布，需保留版权与许可声明；修改过的文件需注明改动；附带专利授权，不提供任何担保。

## 关于数据

仓库**不包含**任何真实业务数据：

- `data/`（SQLite 数据库与每日备份）
- `uploads/`（合同、发票、转账单扫描件）
- `.env`（API Key 与会话密钥）

它们都在 `.gitignore` 里。首次运行 `python run.py` 会自动建表并写入一套演示数据，你自己的数据只保存在本机或 NAS 上。

> 如果 fork 后要提交改动，请先确认 `git status` 里没有 `data/`、`uploads/`、`.env`。