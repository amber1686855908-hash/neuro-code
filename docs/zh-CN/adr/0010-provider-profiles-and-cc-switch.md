# ADR 0010：命名供应商 profile 与可选 CC Switch 兼容

**简体中文** · [English](../../en/adr/0010-provider-profiles-and-cc-switch.md)

状态：已接受

## 背景

Python 第一个纵向切片只使用一个 `[provider.default]` 表，并默认连接 xAI。这会把
供应商身份、线路协议、端点和凭据策略混为一个概念；恢复会话时也无法区分稳定的
供应商 profile 与已经切换上游的网关。

[CC Switch](https://github.com/farion1231/cc-switch) 管理用户提供的供应商凭据/配置，
并可通过回环代理提供协议转换。它不能在没有合法 API Key、OAuth 授权、中继 Token
或本地端点的情况下提供模型访问。若把它设为必需组件，Neuro Code 运行时会耦合另一
应用的进程和私有状态。

## 决策

- 使用命名 `[providers.<name>]` profile、`[routing] default`，以及可选的
  `[routing] fallbacks`。
- 把 profile 身份与线路协议分开：`openai-chat`、`openai-responses`、
  `anthropic-messages` 或 `gemini-generate-content`。供应商专属行为作为可选方言；xAI
  是 Responses 方言。
- 只保存环境变量引用。仅对通过校验的普通 HTTP 回环地址接受 `PROXY_MANAGED`，绝不
  导入其他内联密钥。
- 以最低优先级读取 `NEURO_CODE_CC_SWITCH_CONFIG` 指定的 CC Switch 导出 TOML，
  只把活动 profile 转换为内存配置；不读取其数据库，也不控制其进程。
- 配置解析顺序为：CC Switch/旧用户配置、Neuro Code 用户配置、项目配置、环境变量
  覆盖、CLI 覆盖。
- 没有选中 profile 时仍可检查配置，但模型运行必须返回设置指引；不再存在隐式 xAI
  供应商。
- 新会话持久化不含密钥的 profile 亲和指纹。不透明原生上下文只有在指纹完全匹配时
  才能回放。自动识别的 CC Switch profile 默认禁用原生上下文，因为其上游可能改变。
  没有指纹的旧 Rust/xAI 会话继续执行严格的 xAI 官方 HTTPS 与可信来源标记检查。
- SQLite schema v1 事务化升级到 v2，增加可空亲和字段，不重写旧会话行。

## 影响

Neuro Code 可以直连供应商，也可以选择经过 CC Switch，而不引入强制运行时依赖。用户
可按单次调用选择 profile，配置检查继续保持无密钥输出。旧 TOML 仍可读取，新配置则
不再内置 xAI 默认项。

安全的输出前故障转移现由
[ADR 0011](0011-safe-pre-output-provider-failover.md) 定义。候选项重试、熔断、持久化
健康状态、CC Switch 进程/数据库集成、系统密钥环和 Neuro Code HTTP 代理继续作为
独立的后续工作。
