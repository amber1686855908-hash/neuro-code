# ADR 0128：只读 LSP 纵向切片

## 状态

接受 `codex/lsp-vertical-slice` stacked implementation。本切片刻意保持只读，
不宣称支持工作树、检查点、rename、format、code action 或 workspace edit。

## 决策

Neuro Code 拥有一个 Provider 无关的 `lsp` 工具和一个由应用拥有的
language-server manager。server 按 canonical workspace root 与显式 server profile
路由，不使用进程全局 LSP singleton。profile 通过现有 user/project TOML merge 在
`[lsp.servers.<name>]` 下加载，command 必须是 argv array。Neuro Code 不会安装 server，
也不会根据 language name 隐式推断 executable。

manager 通过 `LocalProcessSandbox`、`LocalProcessPurpose.LSP_SERVER`、protocol stdio、
只读 workspace policy、显式 environment 与有界 process ownership lazy 启动 server。
它实现 Content-Length JSON-RPC 分帧、`initialize`/`initialized`、关联 request、notification、
server-request response、`$/cancelRequest` 以及有界 `shutdown`/`exit` close handshake。
Malformed、oversized 或 truncated frame 会让当前 session 失败，并且不会无界等待。

面向模型的 operation 包括 definition、references、hover、document symbols、workspace symbols、
diagnostics、status 与显式 restart。manager 以 monotonic version 使用 `didOpen`、完整文本
`didChange` 和 `didClose` 同步当前 UTF-8 文档；查询读取当前磁盘内容，不把 watcher 或过期
cache 当作正确性依据。server 宣布 pull 时 diagnostics 使用 pull，否则消费按 URI 保存的有界
publish cache。

模型位置统一为从 1 开始的 Unicode code point line/column。在 protocol 边界转换协商的 server
encoding，默认 UTF-16，并支持 UTF-8/UTF-32。server 返回的 URI 都是不可信输入：只有 local file URI、
canonicalize 后位于配置 workspace roots 内、且不经过 link-like component 的结果才会投影。跨文件
结果继续经过现有 canonical filesystem/permission policy；explicit deny、workspace 外、link-like、
invalid 和未解决的 ask 结果会被省略，不弹出 approval。省略数量保持有界。

server 可以请求 configuration、workspace folders、capability registration 或 message response。
`workspace/applyEdit` 固定回复 not applied，未知 request 返回标准 JSON-RPC method-not-found error。
任何 server result 都不能扩大 Neuro Code filesystem authority、执行 command、应用 edit，或暴露原始
HTML、script、command URI 内容。

## 后果

- 即使没有配置 server，稳定的 tool schema 仍在 execution-time 可见；结果返回 typed failure，
  不会静默从模型 contract 消失。
- startup、initialization、request、diagnostics、stderr、pending request、shutdown、结果数量、
  symbol depth 与文本大小均有界。
- crash session 会产生 typed LSP error，并可在 cooldown 后进行有界次数的 lazy restart。
  Application composition 会在 global background supervisor 之前关闭所有 manager。
- 测试中的真实 fake LSP server 覆盖 framing、Unicode、lifecycle、server request、只读 apply-edit 拒绝、
  malformed/oversized frame、crash、duplicate/late response、timeout 与 stderr pressure。不会下载或要求
  live 第三方 server。

## 不在本 ADR 内

Rename、formatting、code-action execution、`workspace/applyEdit`、任意 server-side file write、自动安装
server、worktree routing、checkpoint/rollback 以及跨进程 detached-descendant ownership 仍待后续工作。
现有 subagent capability invariant 保持不变；LSP 是 parent read-only tool，不会被静默加入固定 child tool list。
