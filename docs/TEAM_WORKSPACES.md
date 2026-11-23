# 团队工作区：配置与运维参考

本改造基于 Open WebUI 0.13.3。新增多企业飞书／Lark 登录、项目、用户 × 项目的持久执行环境、个人 MCP 和套餐账本；保留原有账号、聊天、文件夹、资源 ACL、管理员 Functions 和个人外部终端入口。Open WebUI 的品牌与许可继续适用；执行镜像使用 MIT 许可的 [Open Terminal 0.17.7](https://github.com/open-webui/open-terminal)，归属说明见 `services/sandbox/NOTICE.md`。

第一次接触 Open WebUI，请先按[从零部署指南](DEPLOY_GUIDE.md)完成启动、模型授权、默认套餐、企业登录和普通成员试用。本页供管理员查阅详细规则、升级与恢复；本地测试和上线依赖见[验收记录](TEAM_WORKSPACES_ACCEPTANCE.md)。

## 新部署：先启动执行环境，再配置模型与额度

使用 Linux Docker Engine 和 Docker Compose v2，具体配置和启动命令见[部署指南第 1～3 步](DEPLOY_GUIDE.md#prepare)。控制器必须只有 **一个 worker、一个实例**；它管理本机 Docker，不是跨主机调度器。

`docker-compose.team.yaml` 仅用于**新部署**，默认 PostgreSQL 17 + pgvector、1024 维 `BAAI/bge-m3`、DuckDuckGo 联网搜索（失败时回退到本地 SearXNG）。已有部署按后文升级，不将原向量库直接挂到此模板。

容器运行、存储和搜索带宽有自托管成本，不使用收费的搜索或 Embedding API。

默认成员权限保留模型预设、提示词、知识库、Tools、Skills、分享、Playground、频道、日历和自动化。管理员还可在原有工具连接页配置全局 MCP。模型授权、套餐及企业登录的逐步操作见[部署指南](DEPLOY_GUIDE.md#models)。

模型服务商密钥集中保存在管理员配置中。团队模式拒绝成员已有平台 API Key 的认证，禁止个人模型直连；个人 MCP／OpenAPI／终端的开关与模型直连独立，它们可以有自己的鉴权信息。首次启用团队模式会应用一次默认成员权限；此后的管理员权限调整不会在每次启动时被覆盖。

<a id="enterprise-sso"></a>

## 多飞书／Lark 自定义应用

在飞书／Lark 开放平台创建企业自建应用，发布到所需企业并授予登录所需的基本用户信息权限。每个应用单独增加一条连接：

- 企业显示名称、连接 ID、平台（中国飞书或国际 Lark）、App ID、App Secret。
- 显式允许的 `tenant_key` 列表；不接受通配符。
- 开放平台和后台配置**完全相同**的回调，例如：
  `https://ai.example.com/api/v1/enterprise/company-cn/callback`。
- 按应用实际权限设置 scope；默认 `contact:user.base:readonly`，不要求员工邮箱权限。

登录页按企业选择，跳转该平台的授权／扫码页；回调通过 `code` 换 token，再读取用户信息核对企业和 `open_id`。`state` 有效 5 分钟、绑定发起浏览器 Cookie、只能消费一次，并绑定连接修订版本。

身份键是 **连接 ID + tenant_key + open_id**，内部使用独立用户 ID。相同邮箱不会自动合并，用户可以没有邮箱。多个企业属于同一个协作池，不创建隔离租户。

旧 OAuth 路由保持不变。需要连接已有账号时，由管理员先核实企业目录中的稳定标识，再在企业登录页“绑定已有账号”指定内部用户 ID 和原因。不要通过修改邮箱来合并。修改／停用连接或重新绑定身份会撤销相关账号的旧会话；停用用户沿用原有账号管理和会话撤销能力。

上线前必须使用你们的真实应用验证：不同企业授权／扫码、错误企业、用户停用、连接停用、回调取消、旧账号绑定，以及代理下的 Cookie 和回调地址。自动化测试使用本地身份桩，不代替企业端发布和授权。

## 用户怎么使用

- **项目**：任何普通成员均可创建，负责人邀请指定用户或现有用户组。项目背景说明、模型选择和发布的参考文件共享；新对话默认私有，作者显式切换为项目共享后其他成员才可阅读。项目写权限允许修改资料，不授予读取成员私聊／执行目录的权限。
- **执行环境**：每个内部用户 × 项目一份环境，无项目时使用个人环境。新对话继续使用这个范围的文件；停机和重建保留 `/home/user`，不保留进程内存。不同项目之间不自动同步文件。
- **Python 与文件**：在对话启用代码解释器或选择托管终端，Agent 在该工作区执行。文件面板支持上传、预览、下载；生成图表／Excel 后可显式“发布所选文件到项目”。只导入已授权文件，不挂载整个平台上传目录。发布会创建平台文件引用；撤销分享阻止未来访问／导入，不能撤回成员此前已合法下载的副本。
- **资料索引**：常见文档走原有文本抽取／RAG 流程，处理失败会显示错误；图像、音视频、压缩包作为文件资料发布，不触发额外 OCR／转录服务，也不作为文本索引。
- **手动接管**：交互终端会等待当前 Agent 执行结束，再暂停后续 Agent 队列。完成手工操作后点击“恢复 Agent 队列”。浏览器和桌面操作自己的容器；个人外部环境会标为“用户自管”，不沿用托管环境的目录或凭据。
- **资源和入口**：侧栏突出对话、项目、搜索和额度；“更多”收纳资源、应用和工作环境，可以固定常用入口。个人 Python Tools 的导入、依赖、签名发现和执行都在容器内；共享工具在使用者环境运行，只接收使用者的个人参数，不带作者 Valves。原生 Pipe／Filter／Action 仍只有管理员安装源码，成员可使用和配置个人参数。
- **项目检索**：默认只查当前项目及当前用户可访问的资料／历史；项目对话关闭个人记忆自动读写。旧文件夹仍使用旧共享语义，不被当成项目。

<a id="quota-pricing"></a>

## 套餐与价格

套餐只负责额度，不是成员身份等级，不含购买、支付或充值。未个人指定的用户持续继承全局默认，个人指定优先；可以恢复默认继承。

- 同计量方式、同重置规则（包含时区）立即换基础额度，保留已用、预留、追加和重置时间。基础 1000、已用 600、追加 200，换基础 2000 后剩余 **1600**，下次重置为 **2000**。
- 不同计量方式／周期在当前周期结束时生效。不重置套餐必须明确填写未来生效时间。全局默认的更改不会覆盖已个人指定或已安排的个人套餐。
- 支持自然日、周（周一开始）、月、固定 N 天和不重置，默认 `Asia/Singapore`（UTC+8）；固定 N 天按 24 小时计算。追加额度只属于当期，不结转。
- 追加使用操作 UUID 保证相同请求重试不重复加额；金额、用户、操作人或原因不同不能复用同一个 UUID。管理员调整和异常核对保留审计流水。
- 创建／编辑套餐和修改模型价格均需填写原因；审计记录保留操作人、原因及变更前后的配置。读取成员额度或流水不会显示“已保存”提示。

### 价格表达式

计费标准与 New-API 的表达式计费（`pkg/billingexpr` v1）一致：语法、变量、用量归一化和按次规则都相同，从 New-API 实例、官方倍率预设、models.dev 或 OpenRouter 同步的价格与在 New-API 上计费结果一样。表达式由受限解析器求值，不执行 Python。Token 系数为 **美元／百万 Token**，`fixed(x)` 为每次请求 x 美元；倍率独立填写。计算使用 Decimal，结果四舍五入为整数积分；1 美元等值 = 1,000,000 积分。

```text
# 分类价格；用到的分类互斥，不重复扣普通输入
tier("base", p * 3 + c * 15 + cr * 0.3 + cc * 3.75 + cc1h * 6)

# 整段分档：len 是含缓存的完整输入，不是扣除缓存后的 p
len <= 200000 ? tier("standard", p * 3 + c * 15 + cr * 0.3) : tier("long_context", p * 6 + c * 22.5 + cr * 0.6)

# 按次：每次 0.01 美元；图片按张数
tier("request", fixed(0.01))
tier("image", fixed(0.04)) * image_count

# 请求条件倍率（只读取表达式用到的请求字段，不保存凭据类请求头）
(tier("base", p * 5 + c * 25)) * (param("service_tier") == "fast" ? 2 : 1)
```

变量：`p` 未单列的输入，`c` 未单列的输出，`len` 含缓存的完整输入（用于分档），`cr` 缓存读取，`cc` 缓存写入（短 TTL），`cc1h` Claude 1 小时写入，`cc12h` OpenAI 12 小时写入，`img`／`img_cr`／`img_o` 图片输入／图片缓存读取／图片输出，`ai`／`ao` 音频输入／输出，`image_count` 请求的图片张数（1–128）。函数：`tier`、`fixed`、`param`、`header`、`has`、`hour`／`minute`／`weekday`／`month`／`day`、`max`、`min`、`abs`、`ceil`、`floor`。

用量归一化与 New-API `BuildTieredTokenParams` 相同：

- 表达式没有单列的分类留在 `p`／`c` 内按基础价计费；单列后从 `p`／`c` 中扣除。
- OpenAI 格式：缓存读取取 `cached_tokens`（DeepSeek 取 `prompt_cache_hit_tokens`）；缓存写入取 `cache_write_tokens`、`cached_creation_tokens`、`cache_creation_input_tokens` 中最大的一个，不相加；`cache_write_tokens_details.ttl_12h_tokens` 是 12 小时写入。Responses API 的 `input_tokens_details` 补齐 Chat 细节中缺失的字段。
- Claude 原生格式：输入不含缓存，未单列的缓存读写加回 `p`；写入总数中未标注 TTL 的部分按 5 分钟计。
- 未单列的长 TTL 写入按短写价格折算：Claude 1 小时 ×6/3.75，OpenAI 12 小时 ×2。
- Gemini：`cachedContentTokenCount` 为缓存读取，思考 Token 计入输出，音频／图片按模态拆分。Realtime 只按输入／输出总数结算。
- 服务商报告的缓存数超过输入总数时 `p` 截断为 0（与 New-API 相同）。

按次规则同 New-API：每个执行分支必须是完整的 `tier(名称, fixed(金额))`，只能乘 `image_count` 或请求条件倍率，不能与 Token 价格相加，也不能乘 Token 数。全部分支都是按次价格时，即使上游不返回用量（A1111、ComfyUI、部分图像接口）也会正常结算；上游返回错误时按 0 结算。

模型 ID、价格表达式、倍率、版本以及表达式读取的请求字段随每次出站冻结；后续改价不会改变旧流水。显式 `0` 可用；积分套餐缺价格会在发送前拒绝。

### 同步上游价格

“模型定价”页可从以下来源同步，来源与 New-API 的倍率同步相同：

- New-API 实例的 `/api/pricing` 或 `/api/ratio_config`
- New-API 官方倍率预设（basellm）
- models.dev（同一模型取最便宜的非零输入价）
- OpenRouter（需填写 API Key，仅本次请求使用，不保存）

倍率和按次价格按 New-API 的转换规则生成表达式：倍率 1 = 每百万输入 Token 2 美元；补全倍率使用 New-API 按模型族内置的锁定／兜底值；缓存读取默认 1 倍，缓存写入默认 1.25 倍；Claude 模型总是生成 `cc` 与 `cc1h`；名称含 gpt 且配置了缓存写入的模型同时生成 `cc12h`；图片按次模型乘 `image_count`，DALL·E 附加尺寸／质量倍率。上游已用表达式定价时优先采用表达式。New-API 未知模型的默认倍率（37.5／补全 1）标记为不可信。

同步只做比较，管理员勾选后才写入；每个模型的变更都是一条普通的改价审计记录，并注明来源。

### 请求如何扣量

内置模型 HTTP 出站统一在发送前原子预留：对话、Playground、自动化、重试、工具续轮、后台模型任务、图像／音频推理均归属实际用户，自动化归任务创建者。首轮 + 工具结果续轮 = **2 次**；SSE 分片、Python、MCP、SearXNG 和本地 Embedding 本身不加次数。工具搜索和自动审批若选择 Decisions 模型，每次实际请求也扣量；BM25／本地 Embedding 搜索不加次数。服务商内置的收费搜索／音频调用仍属于模型推理。

发送前失败释放预留；已经发出的失败尝试保留次数。积分按实际 usage 结算；中断、上游报错或缺 usage（全部分支为按次价格时除外）时自动按 0 结算并释放预留，不需要人工处理。实际超过预留仍照实入账，并阻止后续余额不足的请求。跨周期回包结算原周期，删除对话不返还用量。

额度开启时不自动跟随模型 HTTP 重定向，避免在同一预留内静默重发推理；管理员应配置网关的最终 API 地址。服务商返回的 null、布尔值或空字符串用量同样按 0 结算。

输入按请求文本的 UTF-8 字节数预估，输出按请求上限（未指定时 1024）预留；这不是服务商 tokenizer 的精确预算。额度充足仍可能因预留不足触发超支后封锁。

因进程重启或崩溃而一直没有结算的请求，超过 24 小时后由后台任务（启动时和之后每 6 小时）按 0 结算；从没发出的直接释放。

Realtime 开启额度时，平台关闭上游自动 ASR／自动生成，保留 VAD，以显式、分别预留的转录 HTTP 请求和语音 response 计量。单段 PCM 上限 120 秒，积压最多 16 段；录音分段转录也占一次推理请求。

### 管理员原生 Functions 的计量接口

原生 Functions 是可信的后台代码，保留旧插件合同。调用内置 `generate_chat_completion` 已走模型出站门禁；自带 SDK／`requests` 的旧插件需要适配。平台不会拦截任意管理员 Python 网络操作。

Pipe／Filter／Action 可在签名声明 `__model_request__`，每次真实尝试使用一次上下文，包括重试和续轮：

```python
async def pipe(self, body: dict, __model_request__=None):
    async with __model_request__(
        model_id=body['model'], provider='openai',
        url=self.valves.API_URL + '/chat/completions',
        json=body, headers={'Authorization': 'Bearer ' + self.valves.API_KEY},
    ) as response:
        response.raise_for_status()
        return await response.json()
```

流式插件需要使用 `open_webui.utils.session_pool.stream_wrapper(response)` 消费流，才能观察 SSE usage；上下文退出负责最终结算。上线前盘点启用的管理员插件，将其模型请求迁到以上路径；原始网络调用未适配的插件不能算作额度验收通过。

<a id="security-boundaries"></a>

## 沙盒和个人连接的安全边界

默认每容器 1 CPU、2 GiB 内存、128 PIDs；Agent 单次最长 30 分钟（`SANDBOX_EXECUTION_SECONDS`）、空闲 10 分钟停机、全局最多 10 个容器（按 8 核 / 32 GB 设定，可用 `SANDBOX_IDLE_SECONDS`／`SANDBOX_MAX_RUNNING` 调整），超出按 FIFO 排队。内存／swap／CPU 可用 `SANDBOX_MEMORY`／`SANDBOX_SWAP`／`SANDBOX_CPUS` 调整。沙盒按需启动：功能、技能、工作目录、AGENTS.md 等只读探测在沙盒停止时由控制器用停止前刷新的缓存回答，不占名额。单工作区 Agent 串行；打开终端时暂停新执行，终端全部关闭后自动恢复；手动接管则保持到点"恢复 Agent 队列"或沙盒停止。启动失败／超时明确显示并保留卷，不回退到共享环境。

只有控制器持有 Docker Socket。运行容器非 root、移除 Linux capabilities、禁止提权、只读根文件系统，不注入模型密钥、SSO Secret、WebUI 会话或 Docker Socket。短命网络助手仅在启动时设置防火墙，不执行用户代码。控制器 API Key 只在 WebUI 后端使用，不能公开给成员。

运行环境默认离线；用户确认启用公网后，只经代理访问公网 HTTP(S)。代理拒绝私网、云元数据、平台控制面和已配置的平台域名／IP，DNS 解析后固定目标地址。运维必须将平台所有公网别名、直接 IP 和控制面网段填入 `SANDBOX_EGRESS_DENY_HOSTS`／`SANDBOX_EGRESS_DENY_CIDRS`，地址变化同步更新。浏览器看到的仍是同一个隔离容器，不是宿主机桌面。

个人 MCP 默认只接公网，所有 HTTP／OAuth 发现和刷新均检查目标；如确需内网，只在 `PERSONAL_MCP_ALLOWED_PRIVATE_ORIGINS` 添加精确审核过的 origin（协议、主机、端口），例如 `https://mcp.corp.example:8443`，多个用逗号分隔。不要允许平台管理地址或云元数据。连接凭据加密存储；配置更新／禁用／删除会废弃旧 OAuth 身份和缓存，其他用户不能引用该个人连接。原有个人 OpenAPI／终端保留原客户端连接合同；标为用户自管，不把凭据带入共享资源。

<a id="upgrade"></a>

## 现有部署升级与向量索引切换

1. 记录当前应用版本、环境配置、模型／Embedding 配置、数据库种类和卷映射；做下面的一致备份。
2. 在副本运行迁移和关键业务检查，再升级应用。新增 Alembic 版本 `30e4b65c927a`，父版本 `9f6c2a8d1b43`；同时支持 SQLite 和 PostgreSQL。旧文件夹标为 `folder`，新项目才是 `project`，聊天默认 `private`；保留旧账号和 ACL。
3. 为现有部署增加控制器／出口服务并配置 `SANDBOX_CONTROLLER_URL`、`SANDBOX_CONTROLLER_KEY`，不要将 Docker Socket 挂给 WebUI。环境值是首次默认；持久配置已经保存时，以数据库为准。
4. 不自动修改现有 `VECTOR_DB`、Embedding 或索引。要切换 bge-m3／pgvector，先建立**独立新向量库**和匹配维度，使用原文件重建；抽样核对文件数、权限、检索结果后再切换。关系库与向量库可分别配置 `DATABASE_URL`／`PGVECTOR_DB_URL`。
5. 保留旧 Embedding 模型、旧索引和原配置，切换失败时同时恢复它们。不同模型／维度的向量不能混写；不要直接复用现有 collection。
6. 小范围启用真实 SSO、默认套餐与成员沙盒；确认账本、共享、备份恢复后推广。

<a id="backup-restore"></a>

## 一致备份与恢复

备份至少包含：关系数据库、平台 data／uploads（及旧向量库）、控制器 `/state`、本部署所有 `*-home` 持久卷、`.env.team` 和实际密钥／OAuth 加密配置。丢失控制器 state 会丢失用户到卷的映射；丢失 `WEBUI_SECRET_KEY` 无法解密新增 SSO／MCP 凭据。

备份在**同一个停写检查点**完成：

1. 入口进入维护，停止自动化／后台写入，并停止 WebUI；停止控制器防止重新分配。
2. 按本部署的 `owner=SANDBOX_NAMESPACE` 和 `app=open-webui-managed-sandbox` 标签列出运行容器，核对名称后逐个停止。不要停止其他部署容器。停止进程会丢内存，文件仍在卷中。
3. PostgreSQL 使用 `pg_dump -Fc`；SQLite 在应用已停止时备份数据库及其 WAL／SHM，或使用 SQLite 在线备份 API。分别导出平台卷、控制器 state 和上述 home 卷，记录卷名、映射、版本和时间。
4. 密钥备份进入受控密钥存储，不放 Git／工单／公开日志；检查可读性，完成一次独立还原演练后恢复入口。

恢复到同版本、相同 `TEAM_DEPLOYMENT_NAME`／`SANDBOX_NAMESPACE`：保持入口停写，恢复数据库、所有卷和稳定密钥，先启动依赖，再启动控制器和 WebUI；控制器启动会停止自己标签下的旧运行容器并按需重建。验证登录、旧聊天、项目 ACL、额度余额／预留、跨周期流水、工作区文件和下载，再恢复自动化和入口。

需要回退到旧应用时，恢复升级前**整套一致备份**及旧代码／镜像，不只回退前端或关系库。迁移降级在已有项目或额度流水时会拒绝，避免把新项目重新解释为旧共享文件夹、丢失账本。不要为强制降级删除项目或额度记录。

<a id="verification"></a>

## 可复现验证与上线依赖

```bash
# 在已安装项目依赖的 Python 环境，从仓库根目录运行
PYTHONPATH=backend python -m pytest backend/tests/team -q \
  -o asyncio_default_fixture_loop_scope=function --disable-warnings
python backend/tests/team/run_postgres.py

# 先构建 sandbox 的两个镜像；只创建／删除带唯一测试命名空间的 Docker 资源
python -m pytest services/sandbox/tests -q -s \
  -o asyncio_default_fixture_loop_scope=function
```

自动化覆盖精确定价／缓存 TTL／整段分档、并发预留、缺 usage、跨周期、套餐继承与切换、追加幂等、state 过期／重放／错企业、账号绑定、项目私聊、共享工具凭据、文件撤权及个人 MCP 所有权。真实 Docker 检查 CSV → Python → PNG／XLSX、跨用户／项目隔离、停机／重建／控制器重启／空闲停机保卷、资源限制、出口边界、执行超时、手动接管和四容器排队。

本地预览用假模型验证真实 WebUI 出站与界面，不访问付费模型。真实企业应用回调、生产网关 usage（尤其缓存／多模态分类）、已安装管理员插件的出站适配、生产反向代理以及完整备份还原，需由部署方提供实际配置后完成。全项目 `svelte-check` 仍有大量既有类型错误；新增团队页面可构建，但不能将全项目类型检查报告为通过。
