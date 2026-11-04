# 极空间 Z4 Pro 部署与更新指南

> 适用对象：装修项目管理系统（Renovation PMS）
> 目标：把系统跑在极空间 Z4 Pro 上，手机/电脑局域网访问，且**后期升级系统功能尽量省事**。

---

## 0. 为什么这样设计（先看懂再动手）

整套部署围绕一个原则：**代码和数据分开，升级系统=替换代码+重建容器，数据永不丢失。**

| 部分 | 存放位置 | 说明 |
|------|---------|------|
| 代码（程序） | 打进 Docker 镜像 | 升级时替换镜像即可 |
| 数据库 `data/renovation.db` | 挂载到极空间目录 | 容器重建/升级都不丢 |
| 上传文件 `uploads/` | 挂载到极空间目录 | 照片/附件/发票，同样持久化 |

`Dockerfile` 已刻意把「依赖安装」和「源代码复制」分成两层：
- 只要 `requirements.txt` 没变，升级时**不会重新装依赖**，只重打代码层 → 升级快、且依赖稳定
- 数据因为挂在卷里，升级完全不影响

这就是「后期方便更新」的核心。

---

## 1. 前置条件

1. 极空间 Z4 Pro 已启用 **Docker 应用**，且能访问 SSH / 终端（见方式一）或图形界面（见方式二）。
2. 建议在极空间上开启 **SSH**（极空间 App → 系统设置/安全 → 开启 SSH），方便方式一。
3. 准备好本项目 `renovation-pms` 文件夹的完整拷贝。
4. 需要一个**强随机 SECRET_KEY**（用于会话签名，非常重要）。生成方式：
   ```bash
   python -c "import secrets;print(secrets.token_hex(32))"
   ```
   或者直接用 `openssl rand -hex 32`。记下来备用，生产环境必须换掉 `change-me`。

---

## 2. 目录规划（推荐）

在极空间上建一个固定目录，例如：
```
/docker/renovation-pms/          # 放项目代码（docker-compose.yml 在这一层）
/docker/renovation-pms/data/     # SQLite 数据库（自动生成，持久化）
/docker/renovation-pms/uploads/  # 上传文件（照片/附件/发票）
```

> `.env` 文件：建议把 `PROMACLOUD_API_KEY`（AI API Key）也放到极空间上（见后文「敏感信息」），不要写死在镜像里。

---

## 3. 方式一：SSH + Docker Compose（推荐）

> 适合想要**一条命令部署、一条命令更新**的用户。这也是最能发挥「方便更新」优势的方式。

### 3.1 上传项目到极空间

把 `renovation-pms` 目录整体拷贝到极空间，例如上传到 `/docker/renovation-pms`（用极空间文件管理 App，或通过 SMB/WebDAV 拖拽）。

拷贝后目录结构应为：
```
/docker/renovation-pms/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── run.py
├── app/                 # 程序源码
├── data/                # 数据库（可选为空，首启自动建）
└── uploads/             # 上传文件（可选为空）
```

### 3.2 修改密钥（重要）

编辑 `/docker/renovation-pms/docker-compose.yml`，把
`SECRET_KEY=change-me-to-a-long-random-string` 换成你生成的那串随机密钥。

### 3.3 首次构建 + 启动

```bash
# SSH 登录极空间后进入项目目录
cd /docker/renovation-pms

# 后台构建镜像并启动容器（首次会自动建表、写入演示数据）
docker compose up -d --build
```

查看启动日志：
```bash
docker compose logs -f
```
看到 `Application startup complete`、`Uvicorn running on http://0.0.0.0:8018` 即成功。

### 3.4 浏览器访问

- 极空间本机：`http://127.0.0.1:8018`
- 局域网电脑/手机：`http://<极空间IP>:8018`（例如 `http://192.168.1.100:8018`）
- 初始账号：`admin / admin123`（**首次登录后必须立即修改**，操作见第 8 节）

---

## 4. 方式二：极空间 Docker 图形界面（备选）

> 不熟悉命令行时用。极空间 App 的「容器/镜像」功能可手动建容器，把关键映射填对即可。

| 设置项 | 值 |
|--------|-----|
| 镜像 | 用 `python:3.13-slim` 为基础镜像，或直接在极空间「镜像」里从该镜像创建容器 |
| 启动命令 / 入口 | 需在容器内执行 `python run.py`（因极空间界面不便 build 本项目 Dockerfile，通常选「从镜像创建」而不是「构建自定义镜像」） |
| 端口映射 | `8018` → `8018` |
| 存储映射（卷，必填） | `/app/data` → 极空间目录（放数据库）<br>`/app/uploads` → 极空间目录（放上传文件） |
| 环境变量 | `SECRET_KEY=你生成的随机串`<br>`HOST=0.0.0.0`<br>视觉识别（推荐）：`VISION_PROVIDER=zhipu`<br>`VISION_API_KEY=你的密钥`<br>`VISION_MODEL=glm-4.6v-flash` |
| 重启策略 | `总是/unless-stopped`（保证重启不挂） |

> ⚠️ 极空间图形界面**不支持直接 build 本项目 Dockerfile**，它会用基础镜像 + 你的启动命令。这种方式的代码来自镜像，不适合「改代码即更新」。**如果你希望后期方便更新，强烈建议用方式一（SSH + compose）**，方式二仅作临时/不常更新的备用。

---

## 5. 后期更新系统功能（核心）

> 这套设计的最大价值就在这一节。**升级 = 更新代码 + 一条命令重建，数据不动。**

### 5.1 一键更新脚本（推荐）

我提供了一个更新脚本 `update-nas.sh`（见项目根 `deploy/` 或直接放 NAS 项目目录）。在项目目录执行：

```bash
# 首次给脚本执行权限
chmod +x update-nas.sh

# 之后每次升级，先更新代码，再执行：
./update-nas.sh
```

脚本会做三件事：**重新构建镜像 → 重启容器 → 检查运行状态**，并把数据库/上传目录展示给你确认未受影响。

### 5.2 手动更新流程

```bash
cd /docker/renovation-pms

# 步骤1：把新代码同步进来
#    - 若用 git 管理：git pull
#    - 若手动更新：用极空间文件管理，把新版 app/ 等源码覆盖到本目录

# 步骤2：重建镜像 + 重启（依赖没变时很快，只重打代码层）
docker compose up -d --build

# 步骤3：确认状态
docker compose logs --tail 30
docker compose ps
```

### 5.3 依赖（requirements.txt）变了怎么办

如果新版新增/改了 Python 依赖，`--build` 会自动重新 `pip install`。放心执行 `docker compose up -d --build` 即可，首次会多花点时间装依赖。

### 5.4 更新前一定先备份（保命）

任何一次升级前，先备份数据库和上传目录（见第 6 节）。万一出错可回滚。

### 5.5 回滚（上次能用的版本）

```bash
cd /docker/renovation-pms
# 用上一版代码覆盖（或 git checkout 上一 tag）后
docker compose up -d --build
```
数据卷里的数据库/上传不受影响，回滚后数据仍在。

### 5.6 免密部署（本机 → 极空间，无需输密码）

> 2026-09-22 实测可用。`update-to-nas.ps1` 会提示输密码，但极空间的 sudo 策略已对
> `docker` / `cp` / `tar` 开 NOPASSWD，**这三个命令不需要密码**，因此可以全自动完成。

```bash
# 本机：先确认密钥登录与免密规则
ssh -p 10999 -o BatchMode=yes your-nas-user@192.168.1.100 "sudo -n -l"
# 期望看到：NOPASSWD: /usr/bin/docker, /bin/cp, /usr/bin/cp, /bin/tar, /usr/bin/tar
```

完整流程（在项目根执行，注意 tar 排除项——不排除 `dist`/`build` 时包会达 116MB，排除后仅 248KB）：

```bash
TS=$(date +%Y%m%d-%H%M)
tar --exclude='data' --exclude='uploads' --exclude='dist' --exclude='build' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='*.log' --exclude='*.swp' \
    --exclude='*.tar' --exclude='*.tar.gz' --exclude='*.spec' \
    -czf /tmp/update-$TS.tar.gz .

scp -P 10999 /tmp/update-$TS.tar.gz your-nas-user@192.168.1.100:/tmp/

ssh -p 10999 your-nas-user@192.168.1.100 "\
  cd /data_s007/renovation-pms && \
  sudo cp .env .env.bak-$TS && \
  sudo cp docker-compose.yml docker-compose.yml.bak-$TS && \
  sudo cp data/renovation.db renovation.db.bak-$TS && \
  sudo tar -xzf /tmp/update-$TS.tar.gz && \
  sudo docker compose up -d --build && \
  sudo docker compose logs --tail 15 && \
  rm -f /tmp/update-$TS.tar.gz"
```

几个实测要点：

- **解压必须用 `sudo tar`**：`/data_s007/renovation-pms` 对 SSH 登录用户不可写。
  用 `sudo tar` 解压会按包内记录的属主还原，文件仍是 `197108:197121`，与原有文件一致。
- **不要用 `sudo rm` 或在宿主上直接改 uploads**：`rm`/`mkdir` 不在 NOPASSWD 名单里，会卡在密码提示。
  需要清理容器侧文件时，走容器内部（root 身份，且 uploads 已挂载）：
  `sudo docker exec renovation-pms rm -f /app/uploads/bank/xxx.pdf`
- **升级前备份 `.env`**：包内含本地 `.env`，会覆盖 NAS 上的同名文件。
  只要两边 `SECRET_KEY` 一致，覆盖不会导致已登录用户被踢下线；若不一致，所有人需重新登录。
- **数据目录不受影响**：`data/` 与 `uploads/` 是宿主机卷，且打包时已排除。升级后可用
  `stat -c%s data/renovation.db` 对比升级前的字节数确认未变。
- **验证方式**：登录后 `POST /projects/payments/recognize` 上传一张回单，返回
  `{"ok":true,...}` 即识别链路正常；也可在容器内跑 `python scripts/test_vision.py`。

---

## 6. 数据备份（强烈建议）

- 数据库：`/docker/renovation-pms/data/renovation.db`（**建议先停容器再复制**，避免写入中损坏；或用容器内 `sqlite3` 做在线备份）
  ```bash
  docker exec renovation-pms sh -c "sqlite3 /app/data/renovation.db '.backup /app/data/backup-$(date +%F).db'"
  ```
- 上传文件：整个 `uploads/` 目录
- 极空间通常支持「文件夹定时快照/备份」，建议对这两个目录开启。

---

## 7. 敏感信息（API Key、密钥）

- `SECRET_KEY`：用环境变量传入，**不要写进源码或镜像**。
- **视觉识别（转账单图片 / 扫描件合同）：在极空间项目根目录放 `.env` 文件**（与 `docker-compose.yml` 同级），docker compose 自动读取并注入容器。推荐配置：

  ```
  VISION_PROVIDER=zhipu
  VISION_API_KEY=你的密钥
  VISION_MODEL=glm-4.6v-flashx
  ```

  平台可选值与默认模型见 `app/utils/vision_client.py` 的 `PROVIDERS`（智谱 / 阿里云百炼 / 硅基流动 / 火山方舟 / Kimi / OpenAI / 任意 OpenAI 兼容平台 / Proma Cloud 旧配置）。换平台只改这几行，**不需要改代码**。

  **模型选型（2026-09 实测）**：

  | 模型 | 价格 | 实测表现 |
  |---|---|---|
  | `glm-4.6v-flash`（免费） | 0 元 | 高峰时段频繁 429 限流（连测 2 次均失败），识别会随机失败 |
  | `glm-4.6v-flashx`（付费轻量） | 输入 0.15 元/M、输出 1.5 元/M | 稳定 200；**实测单张回单约 0.0006 元**，1 元约可识别 1500+ 张 |

  本项目当前用 `glm-4.6v-flashx`。**是否需要充值**：新注册账号的赠送 token 包已足够（付费模型实测能直接调用并返回用量）；
  只有在赠送额度用尽后才会返回 402 余额不足，届时到智谱控制台充值或用充值包即可，无需改代码。
  客户端已内置 3 次退避重试，偶发限流会自动消化，仍失败时界面会直接显示“触发限流”而不是含糊的“未能识别”。

  **两个已修的真实坑（2026-09-23）**：

  1. **思考链会吃光输出预算**。GLM-4.6 系列默认自动开启 thinking，实测同一张回单：
     开启思考时输出 1683 tokens（其中思维 1427），`max_tokens=1024` 会被思维链占满、
     返回 `finish_reason=length` 与**空内容**（表现为“识别失败”）；关闭思考后只要 245 tokens，
     返回内容完全一致。代码已在智谱 provider 上默认下发 `thinking: {"type":"disabled"}`，
     并把 `max_tokens` 提到 2048 作为兼底。
     ⚠️ 注意：**GLM-5.3 / GLM-5.3-Flash 强制思考、无法关闭**，改用这类模型时要同时去掉该参数。
  2. **一张截图多笔回单**。模型会返回 JSON **数组** `[{...},{...}]`，早期取数逻辑只截“第一个 { 到最后一个 }”，
     把方括号削掉导致非法 JSON。现已支持数组，并取第一笔预填（界面为单笔回单设计）。
     如果一个截图里确实有两笔且你要的是第二笔，请分开截图上传。

  排查建议：遇到识别失败先看界面提示的具体原因，再在容器内跑
  `sudo docker exec renovation-pms python scripts/test_vision.py --image /app/uploads/bank/xxx.png`。

- `PROMACLOUD_API_KEY`：旧配置。不设 `VISION_PROVIDER` 时才会兜底使用它；Proma Cloud 额度耗尽后会直接提示 HTTP 402。保留仅为便于切回。
- 若用极空间图形界面方式（方式二），在容器「环境变量」面板手动加上述 `VISION_*` 变量。**不要把真实 Key 硬编码进代码或提交到公开仓库**。
- 本项目本地 `.env` 若随代码上传到 NAS，同样会被 compose 读取注入，请注意它包含真实 Key，只放在私有 NAS 目录即可。

### 7.1 换平台 / 排查识别问题

在项目目录（本机或 NAS 容器内）执行：

```
python scripts/test_vision.py                 # 诊断：配置是否完整、key 是否有效、额度是否够、模型是否存在
python scripts/test_vision.py --list-models   # 列出当前平台可用模型，用于确认 VISION_MODEL
python scripts/test_vision.py --sample        # 生成合成回单图片并试识别，逐字段比对
python scripts/test_vision.py --image 回单.png  # 用真实回单试识别
```

诊断输出会直接说明失败原因（鉴权失败 / 额度不足 / 限流 / 模型不存在 / 超时），据此就能判断是配置问题还是图片问题。

---

## 8. 客户演示接入（Tailscale Funnel）

> 场景：把系统临时暴露到公网，**客户浏览器直接打开就能看，不需要安装任何客户端**。
> 原理：极空间上的 Tailscale 容器开启 Funnel，把公网 HTTPS 流量转发到本机 8018。

**当前实际参数**（2026-09 现状，换设备后请同步修改）：

| 项 | 值 |
|----|-----|
| Tailscale 节点 | `zspace-ts`（极空间，Linux 容器，**host 网络模式**） |
| Tailscale 容器名 | `tailscale` |
| 公网演示地址 | `https://zspace-ts.tail78c3b9.ts.net` |
| 转发目标 | 本机 `127.0.0.1:8018` |

### 8.1 首次开通（只做一次）

1. 浏览器登录 Tailscale 管理台 → **DNS** 页 → 开启 **HTTPS Certificates**；
   顺手在 **Machines** 页把 `zspace-ts` 的密钥过期关掉（Disable key expiry），
   避免证书到期后演示突然不可用。
2. SSH 登录极空间，执行：
   ```bash
   sudo docker exec tailscale tailscale funnel --bg --https=443 http://127.0.0.1:8018
   ```
3. 首次执行会打印一个授权链接（形如 `https://login.tailscale.com/f/funnel?node=...`），
   用浏览器打开并确认。Tailscale 会自动写入 funnel 节点属性并签发 HTTPS 证书。

### 8.2 验证

- 本机自测：`curl -I http://127.0.0.1:8018` 返回 `303` 即正常（跳转登录页）。
- **公网验证（关键）**：用手机开 4G（不连家里 WiFi、不开 Tailscale）打开
  `https://zspace-ts.tail78c3b9.ts.net`，能出登录页才算成功。
- 注意：公网 DNS 记录**最长需要 10 分钟**才生效。刚开通时外部打不开属正常，稍等再试。

### 8.3 日常开关命令

```bash
# 查看当前状态
sudo docker exec tailscale tailscale funnel status

# 关闭对外暴露（演示完就关）
sudo docker exec tailscale tailscale funnel --https=443 off

# 重新开启
sudo docker exec tailscale tailscale funnel --bg --https=443 http://127.0.0.1:8018
```

带 `--bg` 时配置后台常驻，极空间重启或 `tailscale down/up` 后会自动恢复；
但若 Tailscale 容器被删除重建，需要重新执行一次。

注意：正因为 `--bg` 配置是持久化的，**重启不会关掉它**。想真正关闭只能用上面的 off 命令
（关闭状态同样是持久的，关掉后重启也不会自己开回来）。

### 8.3.1 一键脚本（Windows，双击即用）

项目 `deploy/` 目录下放了三个双击可执行的脚本，不用手敲命令：

| 脚本 | 作用 |
|------|------|
| `deploy/funnel-on.bat` | 开启公网展示链接 |
| `deploy/funnel-off.bat` | 关闭公网展示链接（演示完用） |
| `deploy/funnel-status.bat` | 查看当前 Funnel 配置与容器状态 |

运行时会提示输入极空间的 sudo 密码。若希望完全免密，可在极空间上做一次性配置：

```bash
sudo usermod -aG docker your-nas-user   # 把 SSH 用户加入 docker 组
```

之后 docker 命令不再需要 sudo（注意：加入 docker 组等同于宿主机 root 权限）。

不用电脑时，也可在极空间 App → Docker → 容器 `tailscale` → 终端 里直接执行上面的命令。

### 8.4 演示前必做：修改管理员密码

Funnel 地址是**公网可见**的，任何拿到链接的人都能打开登录页，所以**必须先把默认口令改掉**。

- 系统内路径：登录 → 左侧「办公 / 用户管理」（也可直接访问 `/oa/users`）→ 找到 `admin` → 编辑
  → 填写新密码 → 保存。**密码留空表示不修改**。
- 同时检查是否存在演示账号 `gongcheng` / `caiwu` / `caigou` / `xingzheng`（初始口令 `123456`），
  不需要就在用户管理里停用或删除。
- 登录页已不再显示默认凭据提示；`seed.py` / `run.py` / 客户端启动提示中的明文口令也已一并清理。

### 8.5 注意事项

- **演示完及时关闭**。长期挂着没有收益，只增加暴露面。
- Funnel 有不可配置的带宽限制，纯表单操作够用，图片/附件多时会明显变慢。
- 同一端口不能同时用于 Serve（仅 tailnet 内）和 Funnel，以最后一条命令为准。
- 静态资源走 jsdelivr CDN，不占用 Funnel 带宽。
- 数据（`data/renovation.db`、`uploads/`）在挂载卷里，开关 Funnel 不影响数据。

### 8.6 备选方案：设备分享（客户愿意装客户端时）

- 管理台 → Machines → `zspace-ts` → Share → 按邮箱邀请或复制邀请链接。
- 对方只能看到你分享的这一台设备，看不到 tailnet 内其他机器；
  共享设备默认被隔离（只能被动响应连接），且可随时撤销。
- 对方访问 `http://100.95.70.2:8018`（或 `http://zspace-ts.tail78c3b9.ts.net:8018`）。
- 注意：共享**不会**把极空间的子网路由（`192.168.1.0/24`）带给对方；
  但装修系统就跑在极空间本机 8018 上，所以不受影响。

---

## 9. 移动端表格适配（开发约定）

> 背景：本项目列表页大多是 8～15 列的宽表，桌面端有 `.table-responsive` 可以横向滚动，
> 但在手机上列宽被压扁、内容看不清。现在的做法是：**窄屏下把每行变成一张卡片**
> （字段名在左、值在右），桌面端保持原表格不变。

### 9.1 机制

| 组成 | 位置 | 作用 |
|------|------|------|
| 卡片化样式 | `app/static/css/style.css` 里的 `@media (max-width: 767.98px) { .table-card ... }` | 窄屏隐藏表头、行转卡片、按 `data-label` 显示字段名 |
| 标签自动生成 | `app/templates/base.html` 里的 `pmsFillTableCards` | 把 `thead` 表头文字自动复制到同列单元格的 `data-label` |

因为标签是脚本从表头自动生成的，**新增或修改列表页时通常只需给 `<table>` 加一个 class**，
不必逐个单元格手写 `data-label`。模板里显式写了 `data-label` 的以模板为准（发票列表就用了显式写法）。

筛选/排序引起的局部刷新（`box.innerHTML = ...`）会被 MutationObserver 捕获并自动补全标签，
所以局部刷新后卡片模式不会失效。

### 9.2 新增列表页时怎么做

1. 给表格加 `table-card`：
   ```html
   <table class="table table-hover align-middle mb-0 table-card">
   ```
2. 保持 `<thead>` 里每列都有可读的中文表头（脚本取它的文字当字段名）。
   复选框列、操作列无需处理——表头为空或不在列内时自动不显示标签。
3. 卡片模式下表头会隐藏，因此**依赖表头的功能（列宽拖拽、点击表头排序、表头漏斗筛选）
   在手机上不可用**。如果这些功能必要，请确保页面上另有入口（顶部筛选表单、排序下拉）。

目前已接入 `table-card` 的列表：客户、费用、库存、库存流水、材料（含明细表）、供应商（含付款/对账表）、
项目、合同、采购、分包、公司资料、OA 用户、审计日志、阶段、利润报表、采购汇总、发票。

### 9.3 改了 CSS 必须推新缓存版本号

静态文件**没有 `Cache-Control` 头**，浏览器走启发式缓存（按 `last-modified` 推算），
手机可能几小时内不回源，导致看不到新样式。因此**每次改了 `style.css`，
都要把引用处的版本号改掉**：

```html
<link rel="stylesheet" href="/static/css/style.css?v=20260917">
```

需要同步修改的位置：`app/templates/base.html` 与 `app/templates/login.html`（建议用当天日期）。
改完必须重建容器（CSS/模板都打进镜像）。

---

## 10. 常见问题排查（含「局域网手机无法访问」）

### 10.1 手机/局域网设备打不开
按顺序排查：
1. **端口映射**：确认容器映射是 `8018:8018`（左侧是极空间对外端口，右侧是容器内 8018）。
2. **IP 是否正确**：手机访问 `http://<极空间IP>:8018`，不是电脑/其它设备 IP。极空间 IP 在极空间 App 或路由器后台查看。
3. **是否同一网段**：手机连的 WiFi 要和极空间在同一个局域网（同一个路由器），且路由器未开启「AP/客户端隔离」。
4. **防火墙**：极空间系统若开启防火墙，放行 8018（或按极空间教程开放端口）。部分路由器也需放行。
5. **本机访问 OK 但局域网不行**：多数是防火墙未放行该端口，或客户端隔离。

### 10.2 首页跳转登录页（HTTP 303）
正常现象，说明服务在跑，未登录会重定向到登录页。

### 10.3 容器反复重启/启动失败
```bash
docker compose logs --tail 50
```
常见原因：端口 8018 被占用、`data/` 权限不足（`chmod -R` 或改属主）、`SECRET_KEY` 未设置。

### 10.4 数据库被锁定
SQLite 单写，容器多实例或备份时并发可能导致锁。只跑一个实例，备份用上面 `.backup` 方式，或先停容器再复制。

---

## 11. 总结：一条「升级路径」

```
编辑/拿新版代码 → 拷到 /docker/renovation-pms → ./update-nas.sh → 完成
                                        （或 docker compose up -d --build）
```
数据在卷里，升级不丢；依赖变了自动重装；出问题能回滚。
