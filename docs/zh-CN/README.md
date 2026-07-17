# Neuro Code（PyGrokBuild）

**简体中文** · [English](../en/README.md)

Neuro Code 是一个对开源 Grok Build 终端编码智能体进行独立 Python 重写的项目。
项目使用 Python 原生内部架构，并以 CLI、配置、会话、工具、MCP 和 ACP 等边界上的
可观察行为兼容为目标。

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
PYTHONPATH=src python -m pygrok_build version
PYTHONPATH=src python -m unittest discover -s tests
```

查看已生效且不会暴露密钥的配置：

```bash
PYTHONPATH=src python -m pygrok_build inspect --json
```

使用默认 OpenAI-compatible/xAI 端点运行无头提示：

```bash
export XAI_API_KEY="..."
pygrok-build -p "Explain this repository" --output-format plain
```

Anthropic 和 Gemini 原生流式端点共用同一个运行时。在
`~/.pygrok-build/config.toml` 或项目级同名配置中选择供应商，并填写账户可用的
模型 ID：

```toml
[provider.default]
kind = "anthropic" # 也可以是 "gemini" 或 "openai-compatible"
model = "replace-with-an-api-model-id"
max_output_tokens = 8192
timeout_seconds = 120
```

内置端点与凭据环境变量如下：

| 类型 | 默认基础 URL | 凭据环境变量 |
|---|---|---|
| `openai-compatible` | `https://api.x.ai/v1` | `XAI_API_KEY` |
| `anthropic` | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta` | `GEMINI_API_KEY` |

网关和兼容部署仍可自定义 `base_url` 与 `api_key_env`。Anthropic/Gemini 原生配置
必须显式指定模型，以免把默认 xAI 模型错误发送到其他 API。

恢复、列出、导出和导入会话：

```bash
pygrok-build -p "Continue the work" --resume SESSION_ID
pygrok-build sessions --json
pygrok-build export SESSION_ID --format markdown --output transcript.md
pygrok-build import-session ~/.grok/sessions/ENCODED_CWD/SESSION_ID --json
```

`import-session` 既可以接收上游 Grok Build 会话目录，也可以直接接收其中的
`summary.json`。它只读解析 Rust JSONL 文件，不会修改源文件；随后在单个事务中创建
SQLite 会话，并保留源会话 ID、工作区、模型和时间戳。已有相同会话 ID 时会拒绝导入，
而不是覆盖数据。JSON 报告会列出跳过的损坏记录或暂不支持的记录。规范消息模型目前
尚不能表示推理记录和后端工具记录，导入的图片也暂时使用明确的文本占位符。

无头 Bash 权限支持兼容的 `Bash(...)` 写法。简单命令链中的每个命令都会独立判定，
因此允许 `git status` 不会隐式允许后续命令：

```bash
pygrok-build -p "Inspect the repository" \
  --allow 'Bash(git status)' \
  --deny 'Bash(git push:*)'
```

显式 deny 规则优先于 `--always-approve`。在限制性 Bash 策略下，替换、重定向、
多行脚本以及当前无法安全分解的其他结构会在无头模式下被拒绝。命令使用空 stdin、
有界输出、禁用的分页器/交互提示，并在超时或取消时清理整个进程树。

## 项目状态

- 源代码判定基线：[`xai-org/grok-build`](https://github.com/xai-org/grok-build)
- 固定源提交：`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`
- 最低 Python 版本：3.12
- 目标平台：Linux、macOS、Windows
- 许可证：Apache-2.0；来源要求见 [`NOTICE`](../../NOTICE)

安装后的 `grok` 命令是 `pygrok-build` 的兼容别名。如果系统中已有官方 Grok，建议
将本项目安装在隔离环境中。

本项目与 xAI 或 SpaceXAI 没有关联，也未获得其认可。“Grok”等名称可能是其各自
所有者的商标。
