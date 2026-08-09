"""Provider dialect resolution at the configuration compatibility boundary.

在配置兼容边界解析 Provider 方言.

Runtime providers receive an explicit dialect.  The resolver in this module is
used only when loading configuration written before the dialect field existed.
运行时 Provider 只接收显式方言.本模块仅在加载缺少方言字段的旧配置时使用.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_DEEPSEEK_HOST = "api.deepseek.com"
_DEEPSEEK_MARKER = "deepseek"


def resolve_legacy_dialect(
    *,
    explicit_dialect: str | None,
    provider_name: str,
    protocol: str,
    model: str,
    base_url: str,
    legacy_default_dialect: str = "standard",
) -> str:
    """Resolve a missing legacy dialect without affecting provider runtime.

    显式方言优先;只有旧配置缺少方言时才使用有限、可解释的 DeepSeek 证据.

    The official hostname is the strongest signal.  A clear ``deepseek``
    marker in a historical provider name or model is retained for profiles
    that used a proxy but still carried an explicit DeepSeek identity.  An
    otherwise opaque custom proxy remains standard because its upstream model
    cannot be identified reliably.
    官方主机名是最强信号.对于使用代理但仍保留明确 DeepSeek 身份的旧档案,
    保留供应商名或模型名中的 ``deepseek`` 标识.其他不透明自定义代理无法可靠
    识别上游模型,因此保持 standard.
    """

    if explicit_dialect is not None:
        return explicit_dialect
    if legacy_default_dialect != "standard":
        return legacy_default_dialect
    if protocol != "openai-chat":
        return "standard"

    try:
        hostname = urlsplit(base_url).hostname
    except ValueError:
        hostname = None
    if hostname is not None and hostname.casefold() == _DEEPSEEK_HOST:
        return "deepseek-v4"

    if any(_DEEPSEEK_MARKER in value.casefold() for value in (provider_name, model)):
        return "deepseek-v4"
    return "standard"


__all__ = ["resolve_legacy_dialect"]
