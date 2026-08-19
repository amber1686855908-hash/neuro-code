# ADR 0121：Local Safe Web Fetch

- 状态：已接受；P2 纵向切片
- 日期：2026-08-19
- 范围：公网 HTTP(S) 文本抓取、SSRF 防护、有界提取与 MAIN-facing 路由

## 背景

Hosted Web Search、Anthropic hosted Web Fetch 与 Gemini URL Context 都是
Provider-owned capability。它们必须与 local client 分离；local client 独自拥有
DNS、TCP、TLS、HTTP、redirect、body bound 与 extraction。应用层同时需要一个精简的
model-visible fetch capability，但不能让任意 URL 变成访问内网或外泄凭据的 primitive。

## 决策

1. canonical `WebFetchRequest` 只包含一个 absolute URL 和可选的有界 `max_chars`；
   `WebFetchResult` 只暴露 requested/final URL、title、media type、clean text、status、
   truncation 与 provenance。raw binary、完整 HTML、response headers、cookie 与
   authentication 永远不进入 application 或 tool result。
2. local implementation 是 `WebFetchService` 后面的 `LocalWebFetcher`。它使用
   `aiohttp` 与 custom pinned resolver、`trust_env = false`、无 proxy、dummy cookie
   jar、固定 User-Agent/Accept policy、仅 GET、TLS certificate verification，并为每个
   已校验 hop 使用一个 connector。不复用 Provider HTTP policy。
3. `is_public_destination` 是唯一 address decision。literal 与 DNS A/AAAA result
   都拒绝 loopback、private、link-local、multicast、reserved、unspecified、shared、
   IPv4-mapped-private 及其他 non-global address。只要 DNS 任一 candidate 不安全，就
   拒绝整个 destination。validated result 被 pin 到 TCP connector 实际使用的 resolver，
   防止再次按 hostname lookup 造成 DNS rebinding/TOCTOU gap。
4. 只接受 `http`/`https`，不允许 userinfo，只允许与 scheme 匹配的默认端口 80/443。
   request 前移除 fragment。redirect 手工处理，最多五跳，并对每个 hop 重新校验；
   HTTPS→HTTP downgrade、unsafe destination、unsupported scheme 与 malformed target
   都失败关闭。local client 不发送 credential、cookie、Authorization、Referer 或
   proxy header，因此跨 host 不转发凭据 header。
5. response 在读取前和 streaming 过程中都受 bound。client boundary 设置 header 数、
   line、field limit；`Content-Length` 只做 early guard；解压后的 streamed byte count
   才是权威 body limit。只允许 HTML/XHTML、plain text、Markdown、JSON 与 XML；binary、
   image、audio、video、PDF、octet-stream 与 unknown media 都拒绝。缺失 content type 时
   只做保守的 text/JSON/HTML sniffing。
6. HTML 使用专用、非执行的标准库 `HTMLParser` boundary，丢弃 script/style，输出有界
   clean Markdown。JSON/XML 只作为有界 decoded text 返回，不做 parse 或 entity expansion，
   因此不引入 XXE-capable XML 操作。不支持 JavaScript、browser、PDF、crawling、cache、
   robots、auth、cookie、local network 或非 HTTP 协议。
7. outbound initial 或 redirect URL 中出现 configured redaction value 时，在 DNS/HTTP
   之前返回 `SECRET_IN_URL`，不做替换后发送。fetched title、content、metadata、tool
   result、lifecycle projection 与通用 TUI/JSON/JSONL boundary 再次脱敏。rendered result
   以 `[UNTRUSTED WEB CONTENT]` 开头；这是 provenance boundary，不是已经消除 prompt
   injection 的声明。
8. `[web_fetch] mode` 默认 `disabled`，支持 `disabled`、`local`、`inline`、`auto`。
   `disabled` 不暴露 local 或 MAIN hosted fetch；`local` 移除 MAIN hosted fetch，仅注册
   local tool；`inline` 要求 MAIN 有 explicit supported capability，否则失败关闭；`auto`
   在支持时保留 explicit MAIN hosted fetch，否则使用 local tool。不会因为 Provider 碰巧
   advertise 相关 tool 就启用 capability。search sidecar 内部使用的 hosted fetch 仍由
   Provider 拥有，不作为第二个 main-facing tool 暴露。
9. `web_fetch` 只有在 mode resolution 后才注册，并经现有
   `ToolRegistry` → `ToolExecutor` → permission 路径执行。它标记为 side-effecting，因此
   default headless 会拒绝未匹配的 network read；interactive caller 可以批准，explicit
   permission mode/rule 继续作为 authority。取消会在关闭 request/session boundary 后以
   `CancelledError` 传播，不会转换成普通 tool failure。

## 后果

- DeepSeek/OpenAI-compatible MAIN 在用户显式选择 `local` 或 `auto` 时可以使用 local fetch；
  不增加 DeepSeek-specific Web Fetch Provider。
- fetch contract 与 Provider 无关，未来可增加 trusted proxy seam，同时不会意外继承当前
  Provider proxy environment。
- 第一版没有 cache，也不会自动抓取所有 search result。search→fetch 仍是显式的
  model/application decision。
- TUI、JSON 与 JSONL 接收同一个有界 `ToolResult` projection，不理解 HTTP 或 HTML 语义。

## 验证

Portable tests 覆盖 URL normalization、scheme/port/userinfo、public destination class、
全 candidate DNS validation、resolver pin reuse、redirect 与 downgrade/secret rejection、
streaming 与解压 body bound、MIME/sniffing/charset、HTML extraction、redaction、untrusted
rendering、permission、cancellation、configuration 与 composition ownership。在线 smoke
test 预留给 `NEURO_CODE_LIVE_WEB_FETCH=1`，不需要 Provider key。
