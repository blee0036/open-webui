# Open WebUI 团队版部署指南

本指南带你在一台 Linux 服务器上从零部署团队版 Open WebUI。部署完成后：

- 员工用飞书／Lark 企业账号登录，直接使用管理员接入的模型；
- 员工在项目里上传资料、对话，让 Agent 运行 Python、生成图表和 Excel；
- 管理员给每个人分配套餐，按请求次数或按 Token 金额控制模型用量。

本指南面向**全新部署**。已经在用 Open WebUI 的部署，请按[现有部署升级](TEAM_WORKSPACES.md#upgrade)操作。

整个流程分 9 步：

1. [准备服务器](#prepare)
2. [填写配置并启动](#start)
3. [配置 HTTPS 并创建管理员](#https)
4. [接入模型](#models)
5. [设置套餐和额度](#quota)
6. [接入飞书／Lark 企业登录](#sso)
7. [用普通成员账号试用](#trial)
8. [日常维护](#operations)
9. [常见问题](#troubleshooting)

更细的规则（价格表达式、权限、备份恢复）见[配置与运维参考](TEAM_WORKSPACES.md)。

## 系统由哪些部分组成

| 服务         | 作用                                                                   |
| ------------ | ---------------------------------------------------------------------- |
| `open-webui` | 网页界面和后端：账号、项目、对话、模型连接、额度                       |
| `postgres`   | 数据库：账号、对话、额度账本，以及知识库向量（pgvector）               |
| `sandbox`    | 沙盒控制器：按需为每个“用户 × 项目”创建独立的 Linux 容器，用来运行代码 |
| `egress`     | 出网代理：用户开启公网访问后，沙盒通过它访问外网                       |
| `searxng`    | 自托管搜索引擎，提供联网搜索                                           |

三个自研镜像（WebUI、沙盒控制器、沙盒运行环境）由 GitHub Actions 预编译好，发布在 `ghcr.io/blee0036/`，支持 amd64 和 arm64。服务器只需要拉取，不用自己构建。

<a id="prepare"></a>

## 1. 准备服务器

### 服务器配置

推荐 **8 核 CPU、32 GB 内存**。内存大致这样分配：

| 用途                     | 内存                                              |
| ------------------------ | ------------------------------------------------- |
| 沙盒                     | 每个最多 2 GiB，默认最多同时运行 10 个，约 20 GiB |
| WebUI                    | 1–2 GB                                            |
| 本地 Embedding（bge-m3） | 2–3 GB                                            |
| 数据库和系统             | 剩余部分                                          |

内存更小的机器，按“每个沙盒 2 GiB”调低同时运行的沙盒数 `SANDBOX_MAX_RUNNING`，例如 16 GB 的机器设为 2–4。

### 需要准备的东西

- **软件**：Docker Engine、Docker Compose v2、`openssl`、`git`。
- **域名和证书**：一个员工能访问的 HTTPS 域名，例如 `https://ai.example.com`，以及反向代理（Nginx、Caddy 或公司已有网关）。
- **模型 API**：向网关管理员要三样东西：API 地址（如 `https://gateway.example.com/v1`）、API Key、可用的模型 ID。推荐先接公司已有的 New-API 或其他 OpenAI 兼容网关。

### 下载部署文件

```bash
git clone https://github.com/blee0036/open-webui.git
cd open-webui

# 确认工具都已安装
docker version
docker compose version
openssl version
```

后面所有命令都在这个 `open-webui` 目录下执行。

<a id="start"></a>

## 2. 填写配置并启动

### 2.1 创建配置文件

```bash
cp deploy/team/.env.example .env.team
chmod 600 .env.team
```

运行下面的命令 4 次，得到 4 个不同的随机值：

```bash
openssl rand -hex 32
```

打开 `.env.team`，按下表填写：

| 配置项                      | 填什么                                                                        |
| --------------------------- | ----------------------------------------------------------------------------- |
| `WEBUI_URL`                 | 员工访问的完整地址，如 `https://ai.example.com`（末尾不加 `/`）               |
| `POSTGRES_PASSWORD`         | 第 1 个随机值，数据库密码                                                     |
| `WEBUI_SECRET_KEY`          | 第 2 个随机值，平台主密钥，也用来加密 SSO 和 MCP 凭据                         |
| `SANDBOX_CONTROLLER_KEY`    | 第 3 个随机值，WebUI 调用沙盒控制器时用的密钥                                 |
| `SEARXNG_SECRET`            | 第 4 个随机值，搜索服务密钥                                                   |
| `SANDBOX_EGRESS_DENY_HOSTS` | 员工访问平台用的域名，多个用逗号分隔，如 `ai.example.com`                     |
| `SANDBOX_EGRESS_DENY_CIDRS` | 一般留空。见下方说明                                                          |
| `SANDBOX_MAX_RUNNING`       | 最多同时运行的沙盒数，默认 `10`                                               |
| `SANDBOX_IDLE_SECONDS`      | 沙盒空闲多久后自动停止，默认 `600`（10 分钟）                                 |
| `SANDBOX_ENABLE_BROWSER`    | 工作区自带浏览器（模型的浏览器工具和界面上的浏览器标签页），默认 `false` 关闭 |
| `TEAM_IMAGE_TAG`            | 镜像版本，默认 `latest`。正式环境建议固定为 `sha-<提交号>` 或 `vX.Y.Z`        |

其余配置项保持默认即可：

- `OPEN_WEBUI_PORT=3000`：WebUI 在服务器本机 `127.0.0.1:3000` 上监听，供反向代理连接。
- `TEAM_DEPLOYMENT_NAME`、`SANDBOX_NAMESPACE`：部署名称，只有同一台机器部署多套时才需要改成不同的值。部署后不要再改，数据卷名称依赖它们。
- `PERSONAL_MCP_ALLOWED_PRIVATE_ORIGINS`：留空，员工的个人 MCP 只能连接公网地址。

**关于两个 `SANDBOX_EGRESS_DENY_*`**：沙盒里运行的是用户代码，它们决定这些代码不能访问哪些地址。沙盒默认已经无法访问所有内网地址（`10.x`、`172.16–31.x`、`192.168.x`、`127.x`、云厂商元数据地址等），也只能访问 80 和 443 端口。所以：

- `SANDBOX_EGRESS_DENY_HOSTS` 填平台自己的域名，防止沙盒绕回来访问平台。
- `SANDBOX_EGRESS_DENY_CIDRS` 只在平台有**公网 IP** 时才需要填，写成 `203.0.113.10/32` 这样的格式（`/32` 表示单个 IP），多个用逗号分隔。服务器只有内网 IP、通过 NAT 出网时留空。
- 如果有外部服务按公司出口 IP 设了白名单（比如云 API、合作方接口），沙盒的流量也来自同一个出口 IP，会被放行。把这些服务的域名也加进 `SANDBOX_EGRESS_DENY_HOSTS`。

填好后，把 `.env.team` 备份到安全的地方。以后重启和升级都继续用这份文件，里面的密钥一旦生成就固定下来，不能重新生成。

### 2.2 启动

```bash
# 检查配置。没有输出就是正确的；报错时按提示补齐配置项。
docker compose --env-file .env.team -f docker-compose.team.yaml config --quiet

# 拉取镜像
docker compose --env-file .env.team -f docker-compose.team.yaml pull

# 启动
docker compose --env-file .env.team -f docker-compose.team.yaml up -d

# 查看状态
docker compose --env-file .env.team -f docker-compose.team.yaml ps
```

正常状态：`postgres` 显示 `healthy`，其他 4 个服务显示 `running`。`runtime-image` 只负责拉取沙盒运行镜像，启动后立即退出，所以不在列表里（`ps -a` 中显示为 `Exited (0)`）。用户的沙盒容器在用户第一次运行代码时才创建。

在服务器上确认 WebUI 已经就绪：

```bash
curl -fsS http://127.0.0.1:3000/health   # 返回 {"status":true}
curl -fsS http://127.0.0.1:3000/ready    # 返回 HTTP 200
```

首次启动需要初始化数据库，可能要等一两分钟。一直没有就绪时，看日志找原因：

```bash
docker compose --env-file .env.team -f docker-compose.team.yaml logs --tail=100 open-webui
```

<a id="https"></a>

## 3. 配置 HTTPS 并创建管理员

### 3.1 配置反向代理

员工通过 HTTPS 域名访问平台，反向代理把请求转发到 `127.0.0.1:3000`。登录 Cookie 只在 HTTPS 下生效，所以必须走 HTTPS。

反向代理需要做到三点：转发 WebSocket、关闭响应缓冲（否则对话不会逐字输出）、设置较长的超时。下面是 Nginx 和 WebUI 在同一台机器上的配置示例，替换域名和证书路径即可：

```nginx
# 例如保存为 /etc/nginx/conf.d/open-webui.conf
map $http_upgrade $owui_connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name ai.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name ai.example.com;

    ssl_certificate     /etc/letsencrypt/live/ai.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ai.example.com/privkey.pem;

    # 允许上传的最大文件大小
    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $owui_connection_upgrade;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Nginx 如果运行在容器里，`127.0.0.1` 指的是 Nginx 容器自己，需要改成宿主机的地址。只需要开放 HTTPS 入口，数据库、沙盒控制器等端口都保持在内网。

### 3.2 创建管理员

**新数据库里第一个注册的账号自动成为管理员。** 所以在创建管理员之前，先把域名限制为只有你能访问（例如只对你的 IP 开放），创建完再开放给员工。

1. 浏览器打开 `https://ai.example.com`，看到首次设置页。
2. 填写姓名、邮箱、密码，点击 **创建管理员账号**。
3. 点击左下角头像，进入 **管理员面板**。

这个账号是应急入口，请妥善保管，并保留本地账号密码登录方式。

<a id="models"></a>

## 4. 接入模型

### 4.1 添加模型连接

进入 **管理员面板 → 设置 → 连接**，在 **OpenAI 接口连接** 中添加：

- **URL**：网关的 API 地址，以 `/v1` 结尾，例如 `https://gateway.example.com/v1`。
- **API 密钥**：网关给你的 Key。

点击 **验证连接**，通过后保存。

这里要填网关的最终地址：开启额度控制后，WebUI 不会跟随 HTTP 重定向。

验证一下：回到 **新对话**，选一个模型，发送“请只回复：连接成功”，收到回复就说明接通了。

### 4.2 成员可用的模型

接入的模型**自动对所有登录用户开放**，不需要逐个授权；新对话默认开启联网搜索，由模型自己判断什么时候搜索，成员可以在对话里关掉。代码执行、终端等能力也默认开启，工具调用默认使用原生模式，不需要逐个配置。

需要限制某个模型时（比如只给某个部门用），进入 **资源中心 → 模型**，在该模型的菜单里点 **访问**，改成指定用户或用户组。改过之后系统会保留你的设置，不会再自动改回公开。

想关闭“自动公开”，在 `docker-compose.team.yaml` 的 `open-webui` 服务里加上 `TEAM_PUBLIC_BASE_MODELS: 'false'`，之后新接入的模型只有管理员能用，需要手动授权。

再建一个试点账号，用来在第 7 步以普通成员身份试用：**管理员面板 → 用户 → 添加用户**，角色选 **用户**。

<a id="quota"></a>

## 5. 设置套餐和额度

额度有两种计量方式：

- **按请求次数**：每次调用模型扣 1 次。配置简单，适合先跑通。
- **按 Token 金额**：按每个模型的价格换算成美元扣费，计费规则与 New-API 完全一致（包括缓存读写）。需要先配置模型价格。

### 5.1 创建默认套餐

进入 **管理员面板 → 团队与额度 → 套餐管理**：

1. 在 **新建套餐** 中填写：
   - 名称：`标准套餐`
   - 计量方式：**模型 API 请求次数**
   - 基础额度：`1000`
   - 重置规则：**自然月**
   - 时区：`Asia/Singapore`
2. 点击 **保存套餐**。
3. 在 **默认套餐** 中选择 `标准套餐`，勾选 **启用团队工作区模式** 和 **启用模型额度控制**，点击 **保存配置**。

所有用户自动使用默认套餐。单独给某人指定过套餐后，他就不再跟随默认套餐的变化。

### 5.2 调整个人额度

在 **成员额度** 页搜索用户，可以：

- **分配套餐**：给他单独指定一个套餐，或者选“继承全局默认套餐”改回默认。
- **追加临时额度**：只在本周期有效，下次重置时清零。

成员在侧栏的 **我的额度** 里能看到自己的剩余额度、重置时间和每次请求的明细。明细保留 90 天，过期自动删除，不影响余额；天数可用环境变量 `QUOTA_ENTRY_RETENTION_DAYS` 调整（`0` 表示永久保留）。

一次发送可能产生多次模型请求，例如工具调用后模型继续回答、自动生成标题，每次都会计量。运行 Python、调用 MCP、联网搜索和本地 Embedding 不消耗模型额度。

### 5.3 改用按 Token 计费

1. 进入 **模型价格**，给每个模型配置价格。可以用 **从上游同步价格**，从 New-API 实例、New-API 官方预设、models.dev 或 OpenRouter 导入价格，勾选后应用。
2. 新建一个计量方式为 **按 Token 金额（美元）** 的套餐，基础额度直接填美元金额（例如 `20` 表示每周期 20 美元），分配给用户。

使用按金额计费的套餐时，每个会被调用的模型都必须有价格，没有价格的模型会拒绝请求。免费模型的价格填 `0`。价格表达式的写法见[套餐与价格](TEAM_WORKSPACES.md#quota-pricing)。

<a id="sso"></a>

## 6. 接入飞书／Lark 企业登录

每个企业需要在飞书或 Lark 开放平台创建一个 **企业自建应用**。请对方的应用管理员提供：

- App ID 和 App Secret；
- 企业的 `tenant_key`；
- 确认应用已发布，并已开通 `contact:user.base:readonly` 权限。

员工没有企业邮箱也能登录。

### 6.1 添加企业连接

进入 **管理员面板 → 团队与额度 → 企业登录 → 添加企业连接**，每个企业添加一条：

| 字段              | 示例（中国飞书）                                               | 示例（国际 Lark）                                                  |
| ----------------- | -------------------------------------------------------------- | ------------------------------------------------------------------ |
| 企业名称          | 示例科技 · 中国                                                | 示例科技 · Global                                                  |
| 连接 ID           | `company-cn`                                                   | `company-global`                                                   |
| 平台              | 飞书 / Feishu                                                  | Lark                                                               |
| App ID / Secret   | 该企业应用的 App ID 和 Secret                                  | 该企业应用的 App ID 和 Secret                                      |
| 允许的 tenant_key | 该企业的 tenant_key，每行一个                                  | 该企业的 tenant_key，每行一个                                      |
| 授权范围          | `contact:user.base:readonly`                                   | `contact:user.base:readonly`                                       |
| 回调地址          | `https://ai.example.com/api/v1/enterprise/company-cn/callback` | `https://ai.example.com/api/v1/enterprise/company-global/callback` |

连接 ID 由你自己命名（字母、数字、`_`、`-`），保存后不能修改。勾选 **已启用**，点击 **保存企业连接**。

填写连接 ID 后，回调地址会自动生成。把它原样填到对应应用在开放平台的“重定向 URL”里，两边必须完全一致。修改或停用连接后，这个企业已登录的用户需要重新登录。

### 6.2 测试登录

用浏览器无痕窗口打开登录页，选择企业，点击 **使用企业账号登录**，扫码授权。每个企业都测一遍。

员工第一次登录时，系统自动为他创建账号，角色和套餐使用默认设置，登录后就能直接使用模型。

不同企业的员工可以在同一个项目里协作。邮箱相同的两个账号会保持独立；需要把企业登录绑定到已有账号时，见[身份与旧账号绑定](TEAM_WORKSPACES.md#enterprise-sso)。

<a id="trial"></a>

## 7. 用普通成员账号试用

用第 4 步创建的试点账号（或一个企业登录账号）登录，把下面的流程走一遍。

### 7.1 对话和额度

1. 选择一个模型，正常对话。
2. 打开 **我的额度**，能看到套餐、剩余额度和刚才那次请求。
3. 进入 **项目 → 新建项目**，创建“销售数据分析”，在项目上下文里写一段任务背景。之后在项目里点 **新对话**。

### 7.2 让 Agent 生成图表和 Excel

在电脑上新建 `sales.csv`：

```csv
month,revenue
Jan,120
Feb,180
Mar,240
```

1. 在项目里打开 **我的执行工作区 ↗**，连接选择 **我的托管工作区**，点击启动。首次启动需要等待一会儿。
2. 在 **文件** 面板上传 `sales.csv`，它会出现在沙盒的 `/home/user` 目录。
3. 回到项目对话，选择一个支持工具调用的模型（主流模型都支持），终端连接选择 **我的托管工作区**。
4. 发送：

```text
请实际运行 Python，读取 /home/user/sales.csv，计算 revenue 总额。
生成柱状图 /home/user/revenue.png，以及 Excel /home/user/sales-report.xlsx。
完成后告诉我总额和生成的文件名。
```

模型应回答总额 **540**。在工作区文件面板里能预览和下载 `revenue.png`、`sales-report.xlsx`。刷新页面或在同一项目开新对话，文件都还在。

关于工作区：

- 每个人在每个项目里有自己的工作区，文件只有自己能看到。要给项目成员使用，选中文件后点 **发布所选文件到项目**。
- 项目的 **参考文件** 用于资料问答和检索；**工作区文件** 用于代码执行。Python 只能读到工作区里的文件。
- 工作区默认不能上网。需要下载资料时，点击 **启用公网访问**，此后可以访问公网 HTTP(S) 网站。
- 想自己在终端里操作时，点击 **手动接管**；操作完恢复 Agent 队列。

### 7.3 资料问答、搜索和个人工具

- **资料问答**：在项目的 **参考文件** 上传 PDF 或 Word，处理完成后提问。系统用本地 bge-m3 模型把文档转成向量存入数据库，回答时检索相关段落。图片、音视频和压缩包可以作为文件分享，但不会被识别内容。
- **联网搜索**：管理员先在模型设置里开启联网搜索能力，成员在对话中打开搜索开关，问一个需要实时信息的问题，回答里会附带来源。
- **个人工具**：在 **资源中心** 创建自己的提示词等资源；在 **设置 → 扩展功能** 添加个人 MCP、OpenAPI 工具和终端连接。
- **全员共用的工具**：管理员在 **管理员面板 → 设置 → 扩展功能 → 外部工具服务器** 添加 MCP 服务，并设置访问权限。

最后用第二个成员账号登录，确认看不到第一个人的私有对话和工作区文件，只能看到共享到项目里的内容。全部通过后，就可以开放给更多员工。

<a id="operations"></a>

## 8. 日常维护

```bash
# 查看状态
docker compose --env-file .env.team -f docker-compose.team.yaml ps

# 查看 WebUI 和沙盒控制器日志
docker compose --env-file .env.team -f docker-compose.team.yaml logs --tail=100 open-webui sandbox

# 修改 .env.team 后让配置生效（restart 不会读取新配置，要用 up -d）
docker compose --env-file .env.team -f docker-compose.team.yaml up -d

# 升级：先备份，再拉取新镜像并重建容器，数据会保留
docker compose --env-file .env.team -f docker-compose.team.yaml pull
docker compose --env-file .env.team -f docker-compose.team.yaml up -d
```

### 配置保存在哪里

- `.env.team`：部署相关的配置（密钥、域名、沙盒参数）。
- 数据库：在管理员面板里保存的配置（模型连接、套餐、价格、企业登录）。这些以数据库为准，修改请在管理员面板中进行。

### 数据和备份

所有数据都在 Docker 数据卷里，删除或重建容器不影响数据。需要备份的有：

- 数据库卷
- WebUI 数据卷
- 沙盒控制器状态卷
- 每个用户工作区的 `*-home` 卷
- `.env.team`

备份和恢复的具体步骤见[备份与恢复](TEAM_WORKSPACES.md#backup-restore)。

**注意**：`docker compose down -v` 会删除所有数据卷，排查问题时只用 `down`（不带 `-v`）或 `restart`。

### 沙盒资源

- 每个沙盒最多使用 1 CPU、2 GiB 内存、128 个进程。
- Agent 单次执行最长 120 秒。
- 沙盒空闲 10 分钟（`SANDBOX_IDLE_SECONDS`）后自动停止，文件保留，下次使用时自动启动。
- 最多同时运行 10 个沙盒（`SANDBOX_MAX_RUNNING`），超出的请求排队等待。只有运行代码才会占用名额，单纯聊天不占用。

### 升级建议

- 正式环境把 `TEAM_IMAGE_TAG` 固定到一个具体版本，升级时改成新版本号。
- 把 `.env.team` 里的 `SEARXNG_IMAGE` 固定为验证过的版本（不设置时使用 `latest`）。
- 升级前先备份。涉及知识库向量模型变更的升级，按[升级流程](TEAM_WORKSPACES.md#upgrade)操作。

<a id="troubleshooting"></a>

## 9. 常见问题

| 现象                           | 处理方法                                                                                                                 |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| Compose 提示某个变量为空       | 检查 `.env.team` 里的 4 个随机值、`WEBUI_URL` 和 `SANDBOX_EGRESS_DENY_HOSTS` 是否已填；命令里要带 `--env-file .env.team` |
| 浏览器打不开页面               | 先在服务器上运行 `ps` 和 `curl /ready` 确认服务正常，再检查域名解析、证书和反向代理端口                                  |
| 登录后又跳回登录页             | 用 `WEBUI_URL` 里的 HTTPS 地址访问；检查反向代理是否设置了 `X-Forwarded-Proto`                                           |
| 回复不是逐字输出，或终端连不上 | 检查反向代理的 WebSocket 转发（`Upgrade`、`Connection`）、`proxy_buffering off` 和超时设置                               |
| 成员看不到某个模型             | 该模型的 **访问** 设置被改成了指定用户或组，在 **资源中心 → 模型 → 访问** 里调整                                         |
| 模型返回 401 或 404            | 核对 API Key、API 地址（以 `/v1` 结尾）和模型 ID                                                                         |
| 提示额度不足                   | 在 **我的额度** 查看剩余和重置时间；管理员可在 **成员额度** 追加临时额度                                                 |
| 提示缺少模型价格               | 在 **模型价格** 里给该模型配置价格，免费模型填 `0`                                                                       |
| 飞书／Lark 登录失败            | 核对平台类型、App ID／Secret、tenant_key，确认两边的回调地址完全一致、应用已发布并开通权限                               |
| 工作区一直排队                 | 同时运行的沙盒已达上限。等待其他沙盒空闲停止，或在内存允许时调大 `SANDBOX_MAX_RUNNING`                                   |
| 工作区启动失败                 | 查看 `sandbox` 服务日志，确认沙盒运行镜像已拉取、Docker 正常                                                             |
| Agent 只输出代码，没有生成文件 | 确认模型支持工具调用、对话中选择了 **我的托管工作区**、文件已上传到工作区                                                |
| Python 无法联网                | 工作区默认离线，在工作区点击 **启用公网访问**                                                                            |
| 上传的文档处理失败             | 查看 `open-webui` 日志，确认 Embedding 模型已加载、内存充足                                                              |
