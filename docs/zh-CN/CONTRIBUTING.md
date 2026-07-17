# 为 Neuro Code 贡献代码

**简体中文** · [English](../en/CONTRIBUTING.md)

Neuro Code 是行为重写项目，而不是机械翻译项目。修改兼容敏感的代码路径之前，必须
先在固定的 Rust 源码中找到行为证据，并同步更新兼容矩阵。

## 必须通过的检查

```bash
uv lock --check
uv run python scripts/check_docs_parity.py
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=neuro_code --cov-report=term-missing
uv build
```

所有检查必须在 Python 3.12 上通过。平台相关改动还必须通过 Linux、macOS 和 Windows
CI 矩阵。

## 凭据门控在线检查

pytest 默认会排除带 `live` 标记的测试。运行这些测试必须同时显式选择标记，并打开
费用/网络环境门禁：

```bash
export DEEPSEEK_API_KEY="provided-by-your-secret-manager"
NEURO_CODE_RUN_LIVE_TESTS=1 uv run pytest -m live tests/live
```

不得提交已填写的 `.env`、响应录制、响应转储或失败产物。在线测试必须限制 Token 和
超时时间，不使用破坏性工具，也不得输出凭据、请求头或完整请求体。缺少密钥时应跳过，
而不是失败。`.env.example` 只记录变量名；应用和测试代码都不会隐式加载 `.env`。

## 代码规则

- 领域层和应用层不得导入 UI 框架、供应商 SDK、数据库驱动或平台实现。
- 外部字典必须在适配器处校验；内部模块边界使用带类型的不可变对象。
- 异步任务必须有明确所有者、取消行为和关闭路径。
- 所有副作用必须经过权限系统以及对应的工作区或平台适配器。
- 不得记录凭据、Authorization 请求头、原始密钥文件或完整 HTTP 请求体。
- 显式沙箱配置必须失败关闭。
- 每次行为变更都要包含测试、兼容状态和文档；外部契约变化时还需补充 ADR。
- `docs/en/` 下的每个 Markdown 文件都必须在 `docs/zh-CN/` 的相同相对路径存在
  中文版本，反向亦然。

## 源码来源

上游 Apache-2.0 许可证允许派生作品，但要求保留声明。若代码改编自上游文件，必须
在拉取请求中记录文件路径和提交，并保留适用的第三方署名。在审阅上游
`THIRD-PARTY-NOTICES` 中对应条目之前，不得复制该组件的代码。
