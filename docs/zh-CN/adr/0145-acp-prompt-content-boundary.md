# ADR 0145：ACP Prompt Content 边界提取

- 状态：Accepted
- 日期：2026-08-30
- 范围：V1 Interface Boundary Consolidation 的第一个结构切片
- 依赖：ADR 0035、ADR 0054 和 ADR 0055

## Context

`neuro_code.acp` 仍然是 ACP/JSON-RPC 适配器，并且合理地拥有 connection、session、update、
client capability、MCP 与 transport 生命周期。可是它的入站 prompt/content 校验与转换曾经
和这些职责混在一起，使第一个 interface package consolidation 步骤难以测试，也没有为
content boundary 建立明确 owner。

现有 `neuro_code.interfaces.acp` package 已包含有界 ACP serialization leaves。因此第一个
安全的 consolidation slice 是纯 prompt/content boundary。它必须保留冻结的 ACP 行为，也不能
在其余职责分别迁移前把 `neuro_code.acp` 变成 facade。

## Decision

`neuro_code.interfaces.acp.content` 是入站 ACP prompt/content conversion boundary 的
canonical owner。它拥有：

- `ConvertedPrompt` 与 `PromptBlock`；
- `convert_prompt_content` 及其私有校验/转换 helpers；
- text、image、audio、embedded resource、resource link 和 annotations 的 content-specific
  count 与 byte limits。

`neuro_code.acp` 直接从 canonical module 导入这些 symbol。它不保留第二份实现，也不使用
forwarding wrapper。因此既有 import 继续解析为相同的 function、class、typing alias 与
constant object。

`MAX_RESOURCE_FIELD_BYTES` 明确分类为 `SHARED_ACP_PROTOCOL`，而不是 `CONTENT_BOUNDARY`：
现有 history、event 和 session projection 也使用它。它在已有的
`neuro_code.interfaces.acp.serialization` leaf 中拥有一个与依赖方向无关的唯一 owner，
content module 导入这个共享 limit。它不会重复，也不会被当作 content-only limit。

## 接受的 content 与有界行为

转换按输入顺序接受 ACP `TextContentBlock`、内联 `ImageContentBlock`、内联
`AudioContentBlock`、`ResourceContentBlock` 与 `EmbeddedResourceContentBlock`。Embedded
resource 可以包含已提供的 `TextResourceContents` 或 `BlobResourceContents`。Text、image、
audio、embedded resource、resource link、annotations 以及 prompt aggregate 继续保留原有
count 与 UTF-8/decoded-byte limits。

Image 仍只接受经过校验的 base64 与固定 raster MIME allowlist。Audio 仍只接受经过校验的
base64 与 `audio/*` MIME type。Resource link 与 embedded-resource URI 只作为 metadata：适配器
不会解析、读取、下载或解引用它们。模型可见 projection 会省略 `_meta`。非法输入继续抛出
相同 ACP `RequestError` category 与已观察到的 reason value。

最终有序 `ContentPart` tuple 不变，仍随 user message 传递，使 provider adapter 可以在当前
回合与恢复回合应用自己的 role、MIME 和 request-size 校验。

## Dependency direction

本切片允许的方向是：

```text
neuro_code.acp
        -> neuro_code.interfaces.acp.content
        -> neuro_code.interfaces.acp.serialization / domain conversation types
```

`interfaces.acp.content` 不导入 `neuro_code.acp`、bootstrap 或 infrastructure。它不查找
session、不调用 provider、不执行 resource I/O、不注册 global state，也不协调可变生命周期。

## 明确的非目标

本 ADR 不迁移或重设计 ACP session ownership、connection 或 transport handling、
`NeuroCodeAcpAgent`、`_AcpEventMapper`、history/update projection、client filesystem 或
terminal capability handling、MCP bridging、stdio、WebSocket、TUI、CLI、domain orchestration、
persistence 或任何后续 interface boundary。它不改变 ACP wire shape、limit、accepted type、
error semantics、permission、sandbox behavior 或 provider behavior。

## Compatibility 与分阶段策略

顶层 ACP module 仍是混合适配器。只有本 ADR 中的 prompt/content symbol 在 interface package
中拥有 canonical owner。Compatibility import 保持 identity，architecture test 同时断言该
方向以及不存在重复实现。

未来 ACP consolidation slice 可以在各自完成 audit、compatibility proof 与 behavior-preserving
validation 后，分别为 update、client capability、agent/server handling 和 transport 建立
canonical owner。本 ADR 不预授权这些 slice。

## Validation

验证覆盖既有 ACP content matrix、ACP raw stdio 与 E2E path、dependency 与 import contract、
object identity、documentation parity、完整 repository quality gates，以及最终 pull-request
merge-ref CI。验收标准是结构 consolidation，同时保持可观察 ACP 行为不变。
