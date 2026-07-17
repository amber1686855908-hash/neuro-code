# ADR 0005 — 供应商原生图片回放

**简体中文** · [English](../../en/adr/0005-provider-native-image-replay.md)

## 状态

已接受。

## 背景

导入的上游会话会在独立于供应商的 `ContentPart` 中保留有序文本和图片引用。
把所有图片压平成文本虽然能维持请求有效，却会让恢复后的模型无法查看截图和工具生成
的图片。反过来，把每个已保存字符串都直接当作供应商媒体发送也不安全：格式错误的
data URI、不受支持的媒体类型、过大载荷、带凭据的 URL 以及 API 的角色限制，都可能
使整个下一轮请求失败。

三个适配器采用不同的线上协议。Chat Completions 在用户内容中接收 `image_url`；
Anthropic Messages 在用户消息和工具结果中接收 base64 或 URL 图片块；Gemini
`streamGenerateContent` 接收内联字节或 File API URI，而任意公网 URL 需要单独执行
下载和上传流程。

## 决策

图片引用解析位于供应商适配层，不执行网络或文件系统 I/O。它只接受供应商大小上限
以内、严格有效且非空的 base64 data URI，或具有主机名且不含 URL 用户信息的
HTTP(S) URL。媒体类型会规范化，并通过供应商专属允许列表校验。文本和图片内容项的
原始顺序保持不变。

原生投影有意限定如下：

| 适配器 | 原生输入 | 原生角色 | 明确降级 |
|---|---|---|---|
| OpenAI 兼容/xAI Chat Completions | PNG/JPEG data URI 或公网 HTTP(S) URL，投影为 `image_url` | user | 其他所有角色或无效输入 |
| Anthropic Messages | JPEG/PNG/GIF/WebP base64 或公网 HTTP(S) URL，投影为 `image` | user 和 `tool_result` | system/assistant 或无效输入 |
| Gemini `streamGenerateContent` | 支持的图片 data URI 投影为 `inlineData`，或已有 Gemini File API 资源投影为 `fileData` | user | 任意公网 URL、tool/system/model 角色或无效输入 |

降级时使用现有可见图片占位符，绝不会把原始图片引用复制到模型文本或错误消息中。
适配器不会下载远程图片、读取本地 `file:` URI、上传媒体或修改导入会话。供应商上下文
中的推理和后端工具记录仍属于后续回放工作。

## 影响

受支持的导入截图现在无需改变领域或持久化契约即可送达多模态模型。Anthropic 还保留
了 Rust 基线对工具结果图片的原生处理。无效或不可移植的图片引用不会破坏请求，而会
以明确的媒体丢失边界呈现给模型，不会静默消失。

Gemini 公网 URL 回放和多模态函数响应必须先引入显式媒体传输能力和更丰富的元数据，
才能安全实现。OpenAI 兼容的工具结果图片同样要等到存在可跨网关移植的供应商契约后
再加入。真实模型的限制仍可能比适配器边界更严格，需要通过选择性启用的集成夹具验证。
