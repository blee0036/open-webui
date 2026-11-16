# Open WebUI 团队版部署指南（从零开始）

跟着本指南，你会部署一个公司内部的 AI 工作站：员工选择企业账号登录，直接使用管理员提供的模型，在项目中上传资料、对话，并让 Agent 运行 Python、生成图表和 Excel。员工不需要填写模型 API Key。

本文适用于**本仓库团队改造版本（基于 Open WebUI 0.13.3）的 Linux 单机新部署**。需要使用本仓库的源码和 Compose 文件；直接安装上游的 Python 包或官方 WebUI 镜像，不会包含这些改造。**已经有账号、聊天或知识库的部署，先看[现有部署升级](TEAM_WORKSPACES.md#upgrade)，不要直接套用新装配置。**

## 先认识你会用到的功能

- **模型**负责回答问题。Open WebUI 提供界面与协作能力，模型服务由管理员另行接入，例如公司已有的 New-API 网关。模型调用仍按服务商规则计费。
- **项目**保存任务背景、参考资料和相关对话。项目资料可以共享；对话默认只有作者可见，明确共享后其他项目成员才能查看。
- **工作环境**是真正运行 Python、保存执行文件的 Linux 沙盒。每个“用户 × 项目”各有一份；同一项目的新对话可继续用自己的文件。
- **资源中心**管理模型预设、提示词、知识库、Skills 和 Tools。套餐只控制模型用量，不决定成员能否创建项目或个人资源。

第一次部署按下面的顺序完成；精确定价、权限边界、旧账号绑定及恢复流程放在[配置与运维参考](TEAM_WORKSPACES.md)，测试结果见[验收记录](TEAM_WORKSPACES_ACCEPTANCE.md)。

1. [准备服务器、域名和模型连接](#prepare)
2. [填写配置并启动服务](#start)
3. [接通 HTTPS，创建管理员](#https)
4. [接入模型，并授权给成员](#models)
5. [设置默认套餐和用户额度](#quota)
6. [接入多个飞书／Lark 企业](#sso)
7. [用普通成员账号跑通完整流程](#trial)
8. [了解日常维护与数据保存](#operations)
9. [按现象排查问题](#troubleshooting)

<a id="prepare"></a>

## 1. 准备服务器、域名和模型连接

需要准备：

- **一台 Linux 服务器**，安装 Docker Engine、Docker Compose v2 和 `openssl`。以下命令使用 Bash；Windows 开发机可以用 Docker Desktop 的 Linux 容器，但生产部署采用 Linux。
- **本仓库的完整源码**。进入包含 `docker-compose.team.yaml` 的仓库根目录；后文的 Compose 命令都在这里运行。
- **一个员工可访问的 HTTPS 域名和证书**，例如 `https://ai.example.com`，以及反向代理。已有公司网关可以复用；没有配置时可参考第 3 步的 Nginx 示例。
- **至少一个可用的模型 API**：向网关管理员取得 API 基础地址、管理员使用的 Key、可用模型 ID。第一次建议接公司已有的 OpenAI 兼容网关，先验证一个模型。
- **构建时的网络和磁盘空间**：首次构建会下载基础镜像、Python／前端依赖与 `BAAI/bge-m3` 模型。默认 Embedding 可以在 CPU 上运行，模型推理则由接入的 API 提供。

先检查部署工具和文件：

```bash
docker version
docker compose version
openssl version
ls docker-compose.team.yaml deploy/team/.env.example
```

容量不要只按 WebUI 网页估算。推荐 **8 核 / 32 GB**：默认最多同时运行 10 个沙盒，每个上限 1 CPU、2 GiB（上限不是预留，CPU 共享），满载约 20 GiB；其余留给 WebUI（1–2 GB）、本地 Embedding（bge-m3 约 2–3 GB）、数据库与系统。主机较小时按每个沙盒约 2 GiB 空闲内存调低 `SANDBOX_MAX_RUNNING`，例如 16 GB 主机设为 2–4。镜像由 CI 预编译，服务器无需为构建预留内存；若自行构建，前端构建的 Node 堆上限为 12 GiB，应在其他机器进行。

**创建管理员之前，先把 HTTPS 入口限定为部署人员可访问。** 新数据库的第一个账号会成为管理员，创建完成后再开放给试点员工。

<a id="start"></a>

## 2. 填写配置并启动服务

### 2.1 创建自己的配置文件

```bash
cp deploy/team/.env.example .env.team
chmod 600 .env.team
```

用编辑器打开 `.env.team`。先生成四份**不同的**随机值，分别填入 `POSTGRES_PASSWORD`、`WEBUI_SECRET_KEY`、`SANDBOX_CONTROLLER_KEY` 和 `SEARXNG_SECRET`。下面的命令每运行一次生成一份 64 位十六进制值，运行四次即可：

```bash
openssl rand -hex 32
```

填写配置时按这个表核对：

| 字段                                         | 怎么填                                                                                                           |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `WEBUI_URL`                                  | 员工最终访问的完整 HTTPS 地址，如 `https://ai.example.com`，不带路径或末尾 `/`                                   |
| `POSTGRES_PASSWORD`                          | 第 1 份随机值：数据库密码                                                                                        |
| `WEBUI_SECRET_KEY`                           | 第 2 份随机值：平台稳定密钥，也用于加密新增 SSO／MCP 凭据                                                        |
| `SANDBOX_CONTROLLER_KEY`                     | 第 3 份随机值：WebUI 后端访问沙盒控制器的密钥                                                                    |
| `SEARXNG_SECRET`                             | 第 4 份随机值：自托管搜索服务密钥                                                                                |
| `SANDBOX_EGRESS_DENY_HOSTS`                  | 本平台及管理入口的所有域名，用逗号分隔，如 `ai.example.com,admin.example.com`；不填协议或路径                    |
| `SANDBOX_EGRESS_DENY_CIDRS`                  | 平台／控制面直接 IP 或网段，多个用逗号分隔；IPv4 单 IP 用 `/32`、IPv6 单 IP 用 `/128`                            |
| `TEAM_DEPLOYMENT_NAME` / `SANDBOX_NAMESPACE` | 首套部署可保留 `open-webui-team` / `owui`；同机多套部署必须各不相同，部署后保持稳定                              |
| `OPEN_WEBUI_PORT`                            | 默认 `3000`，是反向代理连接的宿主机本地端口                                                                      |
| `PERSONAL_MCP_ALLOWED_PRIVATE_ORIGINS`       | 首次保持空，个人 MCP 默认仅接公网；确需内网时按[安全边界](TEAM_WORKSPACES.md#security-boundaries)配置精确 origin |

保存并备份 `.env.team` 到受控密钥存储，不提交到 Git。**后续重启或升级继续使用这些密钥，不重新生成。**

### 2.2 检查配置、拉取镜像并启动

WebUI、沙盒控制器和沙盒运行环境三个镜像由 GitHub Actions（`.github/workflows/team-images.yaml`）预编译为 amd64／arm64 双架构，发布在 `ghcr.io/blee0036/`，服务器上不需要构建。`TEAM_IMAGE_TAG` 默认 `latest`（跟随 main），正式环境建议固定为 `sha-<提交>` 或 `vX.Y.Z`，三个镜像共用同一个标签。

```bash
# 校验配置；成功时不输出内容。报错时先补齐对应的配置项。
docker compose --env-file .env.team -f docker-compose.team.yaml config --quiet

# 拉取全部镜像，包括沙盒运行环境（runtime-image 只用于拉取，启动后会立即退出）。
docker compose --env-file .env.team -f docker-compose.team.yaml pull

# 启动常驻服务。
docker compose --env-file .env.team -f docker-compose.team.yaml up -d \
  postgres egress sandbox searxng open-webui

docker compose --env-file .env.team -f docker-compose.team.yaml ps
```

你会看到这些服务：

| 服务名       | 用途                                                        |
| ------------ | ----------------------------------------------------------- |
| `open-webui` | 用户网页、账号、项目、模型连接与额度管理                    |
| `postgres`   | 保存账号、聊天、额度账本，以及 pgvector 知识向量            |
| `sandbox`    | 为每个用户／项目创建和管理独立执行容器                      |
| `egress`     | 用户启用公网访问后，限制沙盒只能访问允许的公网 HTTP(S) 目标 |
| `searxng`    | 自托管联网搜索，不需要第三方搜索 API Key                    |

`postgres` 应显示健康，其他服务应处于运行状态；用户沙盒是在用户使用时才创建的，不会预先出现。若运行环境镜像尚未拉取，控制器会在首次创建沙盒时自动拉取。

需要自行构建（开发或无法访问 GHCR）时，叠加构建文件：`docker compose --env-file .env.team -f docker-compose.team.yaml -f docker-compose.team.build.yaml build`，并把 `TEAM_IMAGE_REGISTRY` 改为 `local` 之类的本地名称，避免覆盖 GHCR 标签。控制器默认只有一个 worker，保持单实例运行。

在**服务器上**检查 WebUI：

```bash
curl -fsS http://127.0.0.1:3000/health
curl -fsS http://127.0.0.1:3000/ready
```

`/health` 返回 `{"status":true}`；`/ready` 返回 HTTP 200 表示应用已就绪。如果还没就绪，先看日志，不要反复重建或删除数据卷：

```bash
docker compose --env-file .env.team -f docker-compose.team.yaml logs --tail=100 open-webui
```

<a id="https"></a>

## 3. 接通 HTTPS，创建管理员

### 3.1 将公司域名代理到 WebUI

默认 WebUI 只监听宿主机的 `127.0.0.1:3000`。员工通过 `WEBUI_URL` 指定的 HTTPS 域名访问，不直接访问这个本地端口；默认登录 Cookie 要求 HTTPS。

已有 Caddy、Traefik 或公司网关时，配置 HTTPS、WebSocket 转发，并关闭对流式响应的缓冲即可。下面是 **Nginx 直接运行在同一台宿主机**时的站点配置示例，需替换域名和证书路径；如果改过 `OPEN_WEBUI_PORT`，同时修改 `proxy_pass` 的端口。

```nginx
# 放在 Nginx http 上下文包含的站点配置中，例如 conf.d/open-webui.conf。
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

    # 本例允许上传最大 100 MiB 的文件，按公司的文件策略调整。
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

如果 Nginx 也在容器内，容器中的 `127.0.0.1` 不是宿主机，需要由部署人员调整代理到 WebUI 的网络连接。不要因此公开数据库、控制器、代理或用户沙盒端口。

Nginx 通过 systemd 管理时，检查并加载配置：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

确认域名解析和证书正确后，在浏览器打开你自己的 `https://ai.example.com`，应看到 Open WebUI 首次设置页。

### 3.2 创建第一个管理员账号

1. 按首次设置页提示填写姓名、邮箱、密码，点击 **创建管理员账号**。
2. 登录后，通过头像菜单进入 **管理员面板**。窄屏上先展开侧边栏，头像菜单在底部。
3. 保留这个账号作为应急管理入口；先不要关闭本地登录。飞书／Lark 登录稍后再配。

本文按简体中文界面描述菜单名称；需要时可在头像菜单的个人设置中切换界面语言。

到这里，网页和数据库已经可用；还需要下一步接入模型，才能真正聊天。

<a id="models"></a>

## 4. 接入模型，并授权给成员

### 4.1 先让管理员完成一次普通对话

进入 **管理员面板 → 设置 → 连接**，在 **管理 OpenAI 接口连接**中添加连接。以 OpenAI 兼容网关为例：

- **URL**：网关管理员给出的 API 基础地址，例如 `https://gateway.example.com/v1`。这里不填 `/chat/completions`，也不填 WebUI 自己的网址。
- **API 密钥**：填网关提供的 Key，由管理员保管。不是 `.env.team` 中的数据库密码或平台密钥。
- 使用 **验证连接**检查，再保存；首次先保留其余高级选项的默认值。

返回 **新对话**，选择刚接入的一个模型，发送“请只回复：连接成功”。能收到正常回答后，再继续配置其他模型。Claude、Gemini 可以通过公司兼容网关接入；使用原生供应商连接时，按该连接的协议与地址配置。

开启额度后，模型请求不自动跟随重定向，因此应使用网关的**最终 API 基础地址**。

### 4.2 再让普通成员看得到这个模型

管理员能看到模型，不代表成员已经被授权。

先在 **管理员面板 → 用户 → 添加用户**创建一个试点账号，角色设为 **用户**。然后进入 **资源中心 → 模型**，在基础模型的菜单中打开 **访问**，给这个用户或所属用户组授予只读权限；确实需要全体登录用户使用时再设为公开。成员创建的个人模型预设只能引用已获授权的基础模型。

后面的 Python 验收需要一个支持工具调用的模型。在该模型的编辑页，将 **高级参数 → 函数调用 (Function Calling)** 设为 **原生模式（Native）**，启用模型的 **终端** 和 **代码解释器**能力，再保存。先验证普通聊天，再验证工具执行，便于区分模型连接与沙盒问题。

<a id="quota"></a>

## 5. 设置默认套餐和用户额度

第一次建议先用“请求次数”套餐：不用配置 Token 价格，容易确认是否接通了额度控制。

进入 **管理员面板 → 团队与额度 → 套餐管理**：

1. 在 **新建套餐**中填写：名称 `标准套餐`、计量方式 **模型 API 请求次数**、基础额度 `1000`、重置规则 **自然月**、时区 `Asia/Singapore`（UTC+8）。填写原因，点击 **保存套餐**。
2. 在 **默认套餐**中选中它，保持 **启用团队工作区模式**，勾选 **启用模型额度控制**，填写原因并点击 **保存配置**。
3. 后续未单独指定套餐的用户会持续继承这个默认套餐；个人指定过的套餐不会被全局默认更改覆盖。

**成员额度**页可以搜索一个用户，然后：

- **分配套餐**：为该用户单独选套餐，或选“继承全局默认套餐”恢复默认继承。
- **追加临时额度**：只加本周期的额度，不改套餐或重置日期；下次重置清除。

成员在侧栏和 **我的额度**查看套餐、剩余量、重置时间、已结算及预留明细。一次点击“发送”不一定只扣一次：工具结果续轮、重试、标题等后台模型任务若实际请求了模型，也会计量。Python、MCP、SearXNG 和本地 Embedding 本身不增加模型请求次数。

以后要改为 Token 积分时，先在 **模型价格**配置每个实际出站模型 ID 的价格，再分配积分套餐。不要直接照抄价格示例作为供应商报价；缺少价格会拒绝发送，明确免费的模型也要填 `0`。换套餐、追加和精确定价规则见[套餐与价格](TEAM_WORKSPACES.md#quota-pricing)。

<a id="sso"></a>

## 6. 接入多个飞书／Lark 企业

每家企业需要自己的飞书或 Lark **企业自建应用**。先请企业应用管理员取得 App ID、App Secret、允许登录的真实 `tenant_key`，发布应用并授予登录所需权限；不要求员工必须有邮箱。

进入 **管理员面板 → 团队与额度 → 企业登录 → 添加企业连接**，每个应用添加一条：

| 字段     | 中国飞书示例                                                   | 国际 Lark 示例                                                     |
| -------- | -------------------------------------------------------------- | ------------------------------------------------------------------ |
| 企业名称 | 示例科技 · 中国                                                | 示例科技 · Global                                                  |
| 连接 ID  | `company-cn`                                                   | `company-global`                                                   |
| 平台     | 飞书 / Feishu                                                  | Lark                                                               |
| 回调地址 | `https://ai.example.com/api/v1/enterprise/company-cn/callback` | `https://ai.example.com/api/v1/enterprise/company-global/callback` |

这里的企业名称和域名是示例，换成你们的真实信息。连接 ID 由你命名，使用字母、数字、下划线或连字符，保存后不能改名。每条连接还要填写：

- 对应应用的 **App ID / App Secret**，不同应用不要混用。
- **允许的企业 tenant_key**：每行一个真实值，不填企业名称、邮箱后缀或 `*`。
- **授权范围**：默认 `contact:user.base:readonly`，与应用已获批权限保持一致。
- 勾选 **已启用**，点击 **保存企业连接**。

把各自的**完整回调地址**登记到对应应用的开放平台配置中，两边必须完全一致。回调由后端接收 `code/state`，换取并核对用户与企业身份；不需要员工手工复制 code。

用无痕窗口打开登录页：先选择企业，再点击 **使用企业账号登录**，完成平台授权／扫码。首次登录会创建内部普通用户并继承默认套餐；管理员再按第 4 步为新成员授权模型。分别测试两家企业，确认进入的是各自的应用，而不是同一个固定授权地址。

多个企业登录后处于同一个协作池，可以按授权共同参与项目；相同邮箱不会自动合并为一个账号。已有账号需要由管理员核实稳定身份后显式绑定，操作方法见[身份与旧账号绑定](TEAM_WORKSPACES.md#enterprise-sso)。修改或停用连接会使相关已有会话和未完成的登录回调失效。

<a id="trial"></a>

## 7. 用普通成员账号跑通完整流程

使用第 4 步创建的普通成员试点账号，或一个已通过企业登录并获得模型授权的普通成员账号。不要只用管理员验收，也不必为了试用开放全员自助注册。

### 7.1 聊天、额度和项目

以普通成员登录，确认：

1. 能选择管理员已授权的模型并完成对话，无需填写模型 Key。
2. **我的额度**显示默认套餐、剩余额度和下次重置时间；请求明细中能看到刚才的模型请求。
3. 进入 **项目 → 新建项目**，创建“销售数据分析”。在项目上下文中填写任务背景，后续从项目内点击 **新对话**。

### 7.2 让 Agent 真正生成图表与 Excel

先在电脑上保存一个名为 `sales.csv` 的文件，内容如下：

```csv
month,revenue
Jan,120
Feb,180
Mar,240
```

然后完成一条最小执行链：

1. 在项目中打开 **我的执行工作区 ↗**，连接选择 **我的托管工作区**。如果环境未启动，点击启动；首次启动可能需要等待。
2. 在 **文件**面板的操作菜单中上传 `sales.csv` 到 Home，即沙盒的 `/home/user`。
3. 返回这个项目的对话，选择支持工具调用的已授权模型，并选择 **我的托管工作区**作为终端连接。
4. 发送下面的指令：

```text
请实际运行 Python，读取 /home/user/sales.csv，计算 revenue 总额。
生成柱状图 /home/user/revenue.png，以及 Excel /home/user/sales-report.xlsx。
完成后告诉我总额和生成的文件名，不要只展示代码。
```

应看到总额 **540**。回到工作区文件面板，预览 `revenue.png` 和 `sales-report.xlsx`，再试着下载。刷新页面、在同一项目开启新对话后，这些文件仍应存在。

执行文件默认只有本人可见。需要让项目成员使用产物时，再选择 **发布所选文件到项目**；共享对话本身不会共享作者的执行目录。**参考文件**用于项目资料与检索，**工作区文件**用于执行；需要 Python 直接读取的文件，要确认它已经在自己的工作区中。

默认环境离线，处理这个 CSV 不需要联网。确需下载公网资料时，由用户明确点击 **启用公网访问**；平台、宿主机、云元数据及未授权内网仍不可访问。手动使用终端时点击 **手动接管**，完成后恢复 Agent 队列。

### 7.3 再验证资料问答、搜索与个人连接

- **资料问答（RAG）**：在项目的 **参考文件**上传一份 PDF／Word 等文档，等待处理完成后提问。这里的 Embedding 是把资料转成可检索的向量，不是训练一个新模型；新部署默认使用本地 `BAAI/bge-m3` 和 pgvector。图像、音视频及压缩包可以作为文件资料分享，但不自动做 OCR／转录或文本索引。
- **联网搜索**：先由管理员确认模型已启用联网搜索能力，再由成员在对话中开启，询问一个需要实时信息的问题并检查来源。新装模板已经连接本地 SearXNG。
- **个人定制**：从头像菜单进入 **资源中心**创建自己的提示词等资源。进入 **设置 → 扩展功能**管理个人 MCP、OpenAPI 和终端连接；这些连接可以有自己的鉴权凭据，与已关闭的个人模型直连不同。

需要全员共用工具时，管理员进入 **管理员面板 → 设置 → 扩展功能 → 外部工具服务器 → 添加连接**，选择 MCP 并配置服务地址、鉴权和访问权限；不要让成员重复填写平台统一管理的连接。

请至少再用一个成员账号检查：对方看不到你的私有对话和执行文件，只能访问你明确共享的项目内容。完成这些检查后，再开放给更多员工。

<a id="operations"></a>

## 8. 了解日常维护与数据保存

常用命令仍在仓库根目录执行：

```bash
# 查看运行状态
docker compose --env-file .env.team -f docker-compose.team.yaml ps

# 查看最近的 WebUI / 沙盒控制器日志
docker compose --env-file .env.team -f docker-compose.team.yaml logs --tail=100 open-webui sandbox

# 修改 Compose 环境变量后重新应用配置；restart 不会更新容器环境变量
docker compose --env-file .env.team -f docker-compose.team.yaml up -d \
  postgres egress sandbox searxng open-webui

# 升级：先备份，再拉取新镜像并重建容器（数据卷保留）
docker compose --env-file .env.team -f docker-compose.team.yaml pull
docker compose --env-file .env.team -f docker-compose.team.yaml up -d \
  postgres egress sandbox searxng open-webui
```

- 后台已保存的模型、套餐、SSO 等配置在数据库中；对应持久配置以数据库为准，不要期待每次改 `.env.team` 都覆盖它们。
- 镜像是程序，**持久卷是数据**。需要备份数据库卷、WebUI data 卷、控制器 state 卷、所有用户 `*-home` 卷，以及稳定密钥。具体停写、备份和恢复顺序见[一致备份与恢复](TEAM_WORKSPACES.md#backup-restore)。
- 不要用 `docker compose down -v` 排错：它会删除本部署的 Compose 命名卷。更改 `TEAM_DEPLOYMENT_NAME` 或 `SANDBOX_NAMESPACE` 也不是恢复数据的方法，原来的卷和工作区映射需要保持一致。
- 沙盒默认 1 CPU、2 GiB、128 PIDs；Agent 单次执行上限 120 秒，空闲 10 分钟停机（`SANDBOX_IDLE_SECONDS`），最多同时运行 10 个容器（`SANDBOX_MAX_RUNNING`），其余排队。名额按“最近空闲时间内用过代码执行的工作区”占用，不是在线人数；只聊天不占名额。停机保留文件，不保留进程内存。
- 正式上线前，将 `SEARXNG_IMAGE` 在 `.env.team` 中固定为你们验证过的版本；模板未设置时使用 `latest`。升级先备份，在副本验证后再发布，已有知识索引的切换按[升级流程](TEAM_WORKSPACES.md#upgrade)处理。

真实企业应用回调、生产网关 usage／价格、已安装管理员 Functions 的模型出站，以及整套备份还原，需要在你们的目标环境中验收；逐项条件见[上线依赖](TEAM_WORKSPACES_ACCEPTANCE.md#production-dependencies)。

<a id="troubleshooting"></a>

## 9. 按现象排查问题

| 现象                       | 先检查什么                                                                                                                     |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Compose 提示变量为空       | 检查 `.env.team` 四个随机值、`WEBUI_URL`、出口拒绝清单；确认命令带 `--env-file .env.team`                                      |
| 浏览器打不开页面           | 在服务器上检查 `ps` 和 `/ready`；服务已就绪时，再检查域名、证书及反向代理的目标端口                                            |
| 登录后又回到登录页         | 使用 `WEBUI_URL` 对应的 HTTPS 地址；检查代理转发协议头，不要直接用 HTTP 本地端口登录                                           |
| 对话断流，终端／桌面连不上 | 检查 WebSocket 的 Upgrade／Connection 转发、代理超时及流式响应缓冲设置                                                         |
| 管理员能聊天，成员没有模型 | 检查基础模型的“访问”授权；接入 API 和授权成员是两个步骤                                                                        |
| 模型报 401／404            | 核对网关 Key、API 基础地址与真实模型 ID；不要把 WebUI 地址当模型地址，也不要重复拼接 `/v1` 或 `/chat/completions`              |
| 额度耗尽或请求被预留挡住   | 打开“我的额度”看已结算、预留和重置时间；管理员按需追加。待核对请求应按服务商凭据处理，不能一律按 0 退款                        |
| 积分套餐提示缺少价格       | 在“模型价格”配置实际出站模型 ID，包含需要计量的后台模型；免费模型明确填 `0`                                                    |
| 飞书／Lark 回调失败        | 核对所选平台、应用的 App ID／Secret、两边完全一致的 HTTPS 回调、真实 tenant_key 和应用发布／授权状态；过期登录从登录页重新发起 |
| 工作区一直排队或启动失败   | 运行数达到 `SANDBOX_MAX_RUNNING` 时等待或停止自己的空闲环境，内存充足可调大该值或调小 `SANDBOX_IDLE_SECONDS`；失败时看 `sandbox` 日志，核对运行镜像已构建、Docker Socket 可用、控制器仍为单实例    |
| Agent 只输出代码，没有文件 | 先确认模型支持工具调用、对话已选择托管工作区、文件已上传到当前用户／项目的执行目录                                             |
| Python 不能联网            | 默认离线；确需公网时显式启用。宿主机、平台控制面、云元数据及未授权内网仍被阻止                                                 |
| 上传文档处理失败           | 查看 WebUI 日志，核对本地 Embedding 模型是否可用、机器容量及文件抽取结果；已有向量库不要直接改模型或维度                       |
