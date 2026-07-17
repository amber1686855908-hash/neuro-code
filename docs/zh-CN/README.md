# Neuro Code

**简体中文** · [English](../en/README.md)

Neuro Code 是一个可扩展的 Python 终端编码智能体。项目使用 Python 原生内部架构，
并以 CLI、配置、会话、工具、MCP 和 ACP 等边界上的稳定行为为目标。

当前实现处于 pre-alpha 阶段。第一个受支持的纵向切片是无头代理运行时；TUI 与协议
集成的进度记录在[兼容矩阵](compatibility-matrix.md)中。

## 开发环境

项目要求 Python 3.12 或更高版本，并统一使用 `uv` 管理环境：

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

在工程引导阶段，也可以不安装软件包而直接运行：

```bash
PYTHONPATH=src python -m neuro_code version
PYTHONPATH=src python -m unittest discover -s tests
```

查看已生效且不会暴露密钥的配置：

```bash
PYTHONPATH=src python -m neuro_code inspect --json
```

## 模型供应商

Neuro Code 不再隐式绑定任何云供应商。必须在 `~/.neuro-code/config.toml` 或
`.neuro-code/config.toml` 中配置至少一个命名 profile；否则模型运行会返回配置指引，
不会静默连接 xAI。

```toml
[routing]
default = "deepseek"
fallbacks = ["anthropic"]

[providers.deepseek]
protocol = "openai-chat"
model = "deepseek-chat"
base_url = "https://api.deepseek.com"
auth = "env"
api_key_env = "DEEPSEEK_API_KEY"
max_output_tokens = 8192
timeout_seconds = 120
proxy_mode = "environment"

[providers.anthropic]
protocol = "anthropic-messages"
model = "replace-with-an-api-model-id"
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
```

支持的线路协议为 `openai-chat`、`openai-responses`、`anthropic-messages` 和
`gemini-generate-content`。配置只保存环境变量名称。Neuro Code 不写入原始 API Key，
不自动读取项目 `.env`，并在检查输出和异常中隐藏凭据。

无需修改默认项即可检查和临时选择 profile：

```bash
neuro-code providers list
neuro-code providers inspect deepseek --json
neuro-code -p "Explain this repository" --provider deepseek
```

`--provider`、`--model` 与 `--base-url` 仅覆盖本次运行。对应的
`NEURO_CODE_PROVIDER`、`NEURO_CODE_MODEL` 和 `NEURO_CODE_BASE_URL` 环境变量会先于 CLI 覆盖生效。
迁移期间仍可读取旧 `[provider.default]` 和 `[model.default]` 配置。

### 安全供应商故障转移

`[routing] fallbacks` 用有序列表声明备用 profile。供应商实例按需创建，因此备用项暂时
不可用不会阻止主 profile 运行。每个候选项至多尝试一次；备用项一旦成功，同一次运行
后续的模型步骤会继续使用它，不会自动切回此前的候选项。

故障转移被严格限制在候选供应商产生第一个模型事件之前。一旦已经出现文本、推理、
工具调用、供应商托管工具生命周期事件、用量或完成事件，后续错误会直接上抛，不会再
尝试其他供应商。这个提交边界避免重复已经产生输出、副作用或费用的工作。尝试失败与
选择结果会分别产生 `provider_attempt_failed` 和 `provider_selected` 运行时事件。可用
`--no-failover` 强制本次运行只使用选中的 profile：

```bash
neuro-code -p "Explain this repository" --no-failover
```

所有候选项均失败时，Neuro Code 会返回有界的汇总错误。目前还不会重试同一候选项、
持久化健康状态或实现熔断。详见
[ADR 0011](adr/0011-safe-pre-output-provider-failover.md)。

### HTTP 代理策略

每个供应商 profile 都有显式 HTTP 传输策略：

- `proxy_mode = "environment"` 是保持兼容的默认值。HTTPX 会读取
  `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 及其证书环境；Neuro Code
  会在所选 profile 启动前按需校验已配置代理 URL 的 scheme。
- `proxy_mode = "direct"` 会设置 HTTPX `trust_env = false`，只对该 profile 忽略代理
  和证书环境变量。
- `proxy_mode = "explicit"` 要求设置 `proxy_url_env`；由指定环境变量提供代理 URL，
  不把它持久化到 TOML。

例如：

```toml
[providers.deepseek]
protocol = "openai-chat"
model = "deepseek-chat"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
proxy_mode = "explicit"
proxy_url_env = "NEURO_DEEPSEEK_PROXY_URL"
```

配置检查只暴露模式、环境变量名和“是否已配置”布尔值；代理 URL 与认证信息会从异常中
脱敏。含义不明确的 `socks://` 会被拒绝，不会擅自猜测；应在安装 HTTPX 可选 SOCKS
依赖后使用 `socks5://` 或 `socks5h://`，或者改用 HTTP 代理。详见
[ADR 0012](adr/0012-provider-http-proxy-policy.md) 和 HTTPX 官方
[环境变量文档](https://www.python-httpx.org/environment_variables/)。

### CC Switch 兼容

将 `NEURO_CODE_CC_SWITCH_CONFIG` 指向 CC Switch 导出的 TOML 文件后，Neuro Code 会
以最低优先级只读加载。活动 `[models] default` 与 `[model."<profile>"]` 会转换为名为
`cc-switch:<profile>` 的内存 profile，映射规则如下：

| CC Switch `api_backend` | Neuro Code 协议 |
|---|---|
| `responses` | `openai-responses` |
| `chat_completions` | `openai-chat` |
| `messages` | `anthropic-messages` |

`env_key` 会作为环境变量引用使用。只有基础地址为普通 HTTP 回环地址（例如
`http://127.0.0.1:15721/provider/v1`）时才接受 `PROXY_MANAGED` 占位凭据。其他内联
密钥不会被复制或使用，对应 profile 会显示为不可用并附带修复提示。CC Switch 始终是
可选项：Neuro Code 不读取其私有数据库、不管理它的进程，直连供应商也不依赖它。
CC Switch 最终仍需合法的上游 API Key、OAuth 授权、中继 Token 或本地模型。

自动识别的代理 profile 默认使用 `native_context = "disabled"`，因为代理可能切换
上游。只有端点和 profile 稳定且可信时，才能在项目覆盖配置中显式设置
`native_context = "profile"`；此时不透明推理也只会在会话中保存的 profile 亲和指纹
完全一致时回放。

### 可选 xAI 方言

xAI 现在是可选 Responses 方言，而不是应用默认项：

```toml
[providers.xai]
protocol = "openai-responses"
dialect = "xai"
model = "replace-with-an-xai-model-id"
base_url = "https://api.x.ai/v1"
api_key_env = "XAI_API_KEY"
native_context = "profile"
builtin_tools = ["web_search", "x_search", "code_interpreter"]
```

通用 Responses 适配器使用本地 `store: false` 历史。xAI 方言会额外请求加密推理、
支持 xAI 托管工具，并保留经过校验的推理/后端工具项目。托管工具由 xAI 执行，不进入
本地工具链；运行时会为其产生独立后端生命周期事件，并且它们可能产生额外费用。目前
尚未实现有状态 `previous_response_id` 链接和压缩项。

DeepSeek 及其他 Chat Completions 服务使用 `openai-chat`。思考模式工具调用关联的
assistant 推理会被持久化并在下一请求回放；没有工具调用的已完成推理只保留在本地。
运行所选 profile 前，应通过 Shell 或密钥管理器加载 `DEEPSEEK_API_KEY`。

### 可选在线回归测试

在线测试需要访问网络并可能产生供应商费用，因此使用两层门禁。普通测试命令会排除
`live` 标记；即使显式选择 `-m live`，如果没有设置
`NEURO_CODE_RUN_LIVE_TESTS=1`，收集阶段仍会把用例标记为跳过。凭据只从当前进程环境读取，
测试套件绝不会自动加载 `.env`。

在调用 Shell 中导出 `DEEPSEEK_API_KEY` 后执行：

```bash
NEURO_CODE_RUN_LIVE_TESTS=1 uv run pytest -m live tests/live
```

可以用 `NEURO_CODE_LIVE_DEEPSEEK_MODEL` 和
`NEURO_CODE_LIVE_DEEPSEEK_BASE_URL` 覆盖 `.env.example` 中的安全默认值。当前 DeepSeek
检查覆盖真实流式响应、从人为制造的主供应商输出前失败中恢复，以及只读本地工具往返；
测试不会启用 Bash 或写入工具。在线测试默认继承标准代理环境变量；设置
`NEURO_CODE_LIVE_PROXY_MODE=direct` 可以忽略它们，或者同时设置
`NEURO_CODE_LIVE_PROXY_MODE=explicit` 与临时环境变量 `NEURO_CODE_LIVE_PROXY_URL`，让代理仅
作用于这些检查。该方式适用于本地 `ALL_PROXY` 使用 HTTPX 拒绝的 URL scheme 的情况；
不能把代理凭据写入项目配置。

恢复、列出、导出和导入会话：

```bash
neuro-code -p "Continue the work" --resume SESSION_ID
neuro-code sessions --json
neuro-code export SESSION_ID --format markdown --output transcript.md
neuro-code import-session /path/to/upstream/session --json
```

`import-session` 既可以接收受支持的上游 Rust 会话目录，也可以直接接收其中的
`summary.json`。它只读解析 JSONL 文件，不会修改源文件；随后在单个事务中创建
SQLite 会话，并保留源会话 ID、工作区、模型和时间戳。已有相同会话 ID 时会拒绝导入，
而不是覆盖数据。JSON 报告会列出跳过的损坏记录或暂不支持的记录。规范消息模型目前
会按原顺序结构化保存推理记录、后端工具记录和图片 URL。JSON 导出格式版本 2 在普通
`messages` 投影之外提供完整的 `conversation_items` 序列。受支持的图片引用会通过
供应商原生内容块回放：OpenAI 兼容和 Gemini 的用户消息，以及 Anthropic 的用户消息
和工具结果。无效引用及不受支持的角色会收到明确图片占位文本。只有来源标记可信且
目标为 xAI 官方 HTTPS 端点时，恢复导入会话才会回放可见推理与有序
后端工具摘要。不透明的 Responses 加密状态绝不会复制到 Chat Completions，非亲和
供应商只接收普通消息投影。改用带 xAI 方言的 `openai-responses` profile 后，会原生
回放经过校验的推理项和受支持的后端工具项，在输入时剥离仅供输出使用的推理状态，并
继续拒绝非亲和的不透明
状态。
旧 assistant 中的 `raw_output`、单体推理和 v0
`reasoning_content` 会在内存中升级；后端工具 ID
可阻止内嵌副本与此前的独立记录重复。导入报告会分别统计恢复项、去重项、损坏项和
不支持的内嵌项。

无头 Bash 权限支持兼容的 `Bash(...)` 写法。简单命令链中的每个命令都会独立判定，
因此允许 `git status` 不会隐式允许后续命令：

```bash
neuro-code -p "Inspect the repository" \
  --allow 'Bash(git status)' \
  --deny 'Bash(git push:*)'
```

显式 deny 规则优先于 `--always-approve`。在限制性 Bash 策略下，替换、重定向、
多行脚本以及当前无法安全分解的其他结构会在无头模式下被拒绝。命令使用空 stdin、
有界输出、禁用的分页器/交互提示，并在超时或取消时清理整个进程树。

## 项目状态

- 最低 Python 版本：3.12
- 目标平台：Linux、macOS、Windows
- 许可证：Apache-2.0；来源要求见 [`NOTICE`](../../NOTICE)
