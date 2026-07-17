# ADR 0012: Per-profile HTTP proxy policy

[简体中文](../../zh-CN/adr/0012-provider-http-proxy-policy.md) · **English**

Status: accepted

## Context

HTTPX trusts standard environment variables by default. This is useful for
corporate networks and local gateways, but one malformed global proxy value can
prevent every provider adapter from being constructed. During the DeepSeek live
regression, an ambiguous `ALL_PROXY=socks://...` value failed before any request
was sent even though a valid HTTP proxy was also available. Silently ignoring or
rewriting global settings would make routing surprising and could bypass a
required security boundary.

Proxy URLs may contain authentication. They therefore require the same
environment-reference and redaction discipline as model API credentials.

## Decision

- Add `proxy_mode` to every named profile:
  - `environment` is the default and keeps HTTPX `trust_env=True`;
  - `direct` sets `trust_env=False` and supplies no proxy;
  - `explicit` sets `trust_env=False` and reads one URL from `proxy_url_env`.
- Store only an environment-variable name for explicit proxies. Never copy a
  resolved proxy URL into inspection, events, sessions, or errors.
- Resolve and validate proxy policy lazily when a candidate adapter is created,
  preserving provider failover when a fallback has missing proxy configuration.
- Pass one immutable `HttpClientPolicy` to all four wire adapters. It owns HTTPX
  client options and redaction of API/proxy secrets.
- Validate standard environment proxy URLs in environment mode. Report the
  variable and problem category, never the URL or endpoint credentials.
- Reject ambiguous `socks://` rather than infer a protocol version. Accept
  `socks5://`/`socks5h://` only when HTTPX's optional SOCKS dependency is
  installed. HTTP and HTTPS proxy URLs remain supported by the core install.
- `direct` deliberately also ignores other HTTPX environment configuration,
  including certificate variables. Users requiring custom trust roots should
  use environment mode until an explicit TLS policy is designed.

## Consequences

Users can isolate one provider from broken global proxy state or route it
through a dedicated secret-bearing proxy without affecting other profiles.
Configuration remains portable and safe to inspect. Invalid proxy state becomes
an actionable configuration failure and can participate in pre-output provider
failover.

PAC evaluation, separate HTTP/HTTPS mounts, proxy rotation, bundled SOCKS
dependencies, and explicit TLS/CA configuration remain future work.
