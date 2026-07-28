# ADR 0046：全局 CLI 与受管供应商设置

**简体中文** · [English](../../en/adr/0046-global-cli-and-managed-provider-settings.md)

- 状态：已接受
- 日期：2026-07-22

## 背景

普通安装应当无需激活虚拟环境，就能从任意目录启动 TUI。TUI 还需要为未配置的安装提供
首次设置，并允许用户在不编辑 TOML、不导出 API 密钥环境变量的情况下管理多个供应商
profile。既有手工 TOML 和环境变量 profile 必须继续有效，工作区也不能重定向用户保存
的密钥。

## 决策

- 发布 `neuro` 和 `neuro-code` 控制台脚本，并接受 `code` 作为显式 TUI 子命令。所有无
  prompt 形式仍以 `Path.cwd()` 作为默认工作区。Textual 成为普通依赖，因此通过
  `uv tool install` 或 `pipx install` 可得到完整的全局 TUI 命令。
- 增加 `ProviderSettingsStore` 端口和 JSON 适配器。经过校验的供应商元数据写入
  `providers.json`，API 密钥单独写入 `credentials.json`；两者使用原子替换和仅所有者可
  访问的 POSIX mode。含密钥的 dataclass 字段不参与对象表示和比较。
- 在 TOML 之后加载受管 profile。同名受管 profile 完整替换供应商表，绝不继承项目的
  端点、代理、内建工具或认证选项。
- 在 `ApplicationComposition.open` 之前执行首次供应商设置。普通设置入口先显示一级分类，
  再分别进入语言或供应商详情；首次使用则直接进入必需的供应商表单。“保存并使用”先关闭
  活动组合和后台 scope，再通过有界 TUI 重启码重新加载配置。选择已有受管 profile 时会
  持久保存默认项。
- 供应商预设按线路行为命名，不把所有兼容 OpenAI SDK 的服务当成同一种协议。
  `OpenAI Responses` 选择 `/responses`，`兼容 Chat` 选择 `/chat/completions`，专用
  DeepSeek 预设则使用 Chat Completions 与 `https://api.deepseek.com`。
- 存储端口提供原子 profile 删除，使 profile 与单独保存的凭据条目一并移除；本切片延后
  删除界面，后续由带确认流程的
  [ADR 0047](0047-recoverable-managed-provider-proxy-settings.md)补齐。
- 把已配置凭据值放入不会出现在对象表示中的 `ToolContext.redaction_values`，并在真实工具
  结果进入模型上下文、事件或持久化之前执行脱敏。

## 影响

- 手工 `~/.neuro-code/config.toml` 和环境变量密钥 profile 继续受支持，显式 CLI 供应商
  覆盖在该次启动中仍然优先。
- 当前凭据文件是私有文件，但不会做静态加密。端口设计允许以后替换为操作系统钥匙串，
  而无需修改 TUI 或应用契约。
- 远程模型发现、原生安装程序和操作系统钥匙串迁移不在本切片范围内；确认删除与代理恢复
  由[ADR 0047](0047-recoverable-managed-provider-proxy-settings.md)覆盖。
