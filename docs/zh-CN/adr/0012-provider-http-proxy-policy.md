# ADR 0012：按 profile 配置 HTTP 代理策略

**简体中文** · [English](../../en/adr/0012-provider-http-proxy-policy.md)

状态：已接受

## 背景

HTTPX 默认信任标准环境变量，这对企业网络和本地网关很有用；但一个格式错误的全局代理
值也可能让所有供应商适配器都无法构造。在 DeepSeek 在线回归中，含义不明确的
`ALL_PROXY=socks://...` 在任何请求发出前就导致失败，即使环境里同时存在有效 HTTP
代理。静默忽略或改写全局设置会让路由行为不可预测，也可能绕过必需的安全边界。

代理 URL 可能携带认证信息，因此必须采用与模型 API 凭据相同的环境变量引用和脱敏
规则。

## 决策

- 为每个命名 profile 增加 `proxy_mode`：
  - `environment` 是默认值，保留 HTTPX `trust_env=True`；
  - `direct` 设置 `trust_env=False`，且不提供代理；
  - `explicit` 设置 `trust_env=False`，并从 `proxy_url_env` 读取一个 URL。
- 显式代理只保存环境变量名。解析后的代理 URL 绝不进入配置检查、事件、会话或错误。
- 候选适配器创建时才解析并校验代理策略，使备用 profile 缺少代理配置时仍可参与供应商
  故障转移。
- 向四种线路适配器传入同一个不可变 `HttpClientPolicy`，由它统一提供 HTTPX 客户端
  选项并脱敏 API/代理秘密。
- 环境模式会校验标准代理环境变量。错误只报告变量名和问题类别，不报告 URL 或端点
  凭据。
- 拒绝含义不明的 `socks://`，不猜测协议版本。只有安装 HTTPX 可选 SOCKS 依赖后才
  接受 `socks5://`/`socks5h://`；核心安装继续支持 HTTP 和 HTTPS 代理 URL。
- `direct` 也会有意忽略其他 HTTPX 环境配置，包括证书变量。需要自定义信任根的用户
  在显式 TLS 策略完成前应使用环境模式。

## 影响

用户可以隔离某个供应商与损坏的全局代理状态，或让它使用专属且含秘密的代理，而不
影响其他 profile。配置继续保持可移植且可安全检查。无效代理状态会变成可操作的配置
错误，并且可以参与输出前供应商故障转移。

PAC 解析、独立 HTTP/HTTPS 挂载、代理轮换、内置 SOCKS 依赖和显式 TLS/CA 配置继续
作为后续工作。
