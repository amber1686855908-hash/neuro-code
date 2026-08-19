"""Canonical provider-service metadata and capability resolution.

定义规范的 Provider 服务元数据和能力解析.

This module deliberately describes services rather than constructing providers.
Wire adapters remain owned by infrastructure and are selected by protocol.
本模块只描述服务,不创建 Provider. 协议适配器仍由基础设施层按 wire protocol 负责.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import urlsplit

from neuro_code.application.ports.model import (
    CapabilityResolution,
    ModelCapability,
    ModelCapabilitySet,
    resolve_capabilities,
)
from neuro_code.shared.errors import ConfigurationError

SUPPORTED_PROTOCOLS = frozenset(
    {
        "openai-chat",
        "openai-responses",
        "anthropic-messages",
        "gemini-generate-content",
        "gemini-interactions",
    }
)
SUPPORTED_DIALECTS = frozenset({"standard", "xai", "deepseek-v4"})
_MAX_SERVICE_ID_CHARACTERS = 128
_MAX_URL_CHARACTERS = 2_048


class CredentialStyle(StrEnum):
    """Non-secret description of how a service is authenticated."""

    API_KEY = "api-key"
    OAUTH = "oauth"
    PROXY_MANAGED = "proxy-managed"
    UNSUPPORTED_INLINE = "unsupported-inline"


class ModelCatalogStrategy(StrEnum):
    """Model-discovery strategy selected by service metadata."""

    OPENAI_COMPATIBLE = "openai-compatible-models"
    ANTHROPIC = "anthropic-models"
    GEMINI = "gemini-models"
    STATIC = "static"
    MANUAL_ONLY = "manual-only"


@dataclass(frozen=True, slots=True)
class ProviderPublisherMetadata:
    """Optional publisher metadata; it has no runtime ownership."""

    publisher_id: str
    display_name: str

    def __post_init__(self) -> None:
        if not self.publisher_id.strip() or not self.display_name.strip():
            raise ConfigurationError("provider publisher metadata must be non-empty")


def _normalized_url(value: str) -> str:
    return value.strip().rstrip("/").casefold()


def _validate_default_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        return normalized
    if len(normalized) > _MAX_URL_CHARACTERS:
        raise ConfigurationError("provider service default base URL is too long")
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise ConfigurationError("provider service default base URL is invalid") from error
    if parsed.scheme not in {"http", "https"} or hostname is None:
        raise ConfigurationError(
            "provider service default base URL must be an absolute HTTP(S) URL"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("provider service default base URL must not contain user info")
    if parsed.query or parsed.fragment:
        raise ConfigurationError(
            "provider service default base URL must not contain a query or fragment"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class ProviderServiceDescriptor:
    """Immutable metadata for one selectable inference service.

    ``service_id`` is the service identity.  ``default_protocol`` and dialect
    metadata select a wire adapter, while capability metadata is only a
    conservative input to effective-capability resolution.  No credential is
    stored here.
    """

    service_id: str
    display_name: str
    default_protocol: str
    default_base_url: str
    supported_protocols: frozenset[str] = field(default_factory=frozenset)
    default_dialect: str = "standard"
    dialect_by_protocol: Mapping[str, str] = field(default_factory=dict)
    credential_style: CredentialStyle | str = CredentialStyle.API_KEY
    model_catalog_strategy: ModelCatalogStrategy | str = ModelCatalogStrategy.MANUAL_ONLY
    capabilities: ModelCapabilitySet = field(default_factory=ModelCapabilitySet.all_unknown)
    protocol_capabilities: Mapping[str, ModelCapabilitySet] = field(default_factory=dict)
    model_capabilities: Mapping[str, ModelCapabilitySet] = field(default_factory=dict)
    model_capabilities_by_protocol: Mapping[str, Mapping[str, ModelCapabilitySet]] = field(
        default_factory=dict
    )
    publisher: ProviderPublisherMetadata | None = None
    ui_key: str | None = None
    aliases: tuple[str, ...] = ()
    label_key: str | None = None
    model_placeholder_key: str | None = None
    protocol_hint_key: str | None = None

    def __post_init__(self) -> None:
        service_id = self.service_id.strip()
        if not service_id:
            raise ConfigurationError("provider service_id must not be empty")
        if len(service_id) > _MAX_SERVICE_ID_CHARACTERS:
            raise ConfigurationError("provider service_id is too long")
        if not self.display_name.strip():
            raise ConfigurationError("provider service display_name must not be empty")
        if self.default_protocol not in SUPPORTED_PROTOCOLS:
            raise ConfigurationError(
                f"unsupported provider service protocol: {self.default_protocol}"
            )
        supported = frozenset(self.supported_protocols or {self.default_protocol})
        if not supported.issubset(SUPPORTED_PROTOCOLS):
            unsupported = sorted(supported - SUPPORTED_PROTOCOLS)
            raise ConfigurationError(f"unsupported provider service protocols: {unsupported}")
        if self.default_protocol not in supported:
            raise ConfigurationError("provider service default protocol must be supported")
        dialect_by_protocol = dict(self.dialect_by_protocol)
        if self.default_protocol not in dialect_by_protocol:
            dialect_by_protocol[self.default_protocol] = self.default_dialect
        if not set(dialect_by_protocol).issubset(supported):
            raise ConfigurationError(
                "provider service dialect metadata has an unsupported protocol"
            )
        if any(dialect not in SUPPORTED_DIALECTS for dialect in dialect_by_protocol.values()):
            raise ConfigurationError("provider service has an unsupported dialect")
        if self.default_dialect not in SUPPORTED_DIALECTS:
            raise ConfigurationError("provider service has an unsupported default dialect")
        if dialect_by_protocol[self.default_protocol] != self.default_dialect:
            raise ConfigurationError(
                "provider service default dialect does not match its default protocol"
            )
        try:
            credential_style = CredentialStyle(self.credential_style)
            catalog_strategy = ModelCatalogStrategy(self.model_catalog_strategy)
        except ValueError as error:
            raise ConfigurationError(
                "provider service metadata contains an unsupported enum"
            ) from error
        ui_key = (self.ui_key or service_id).strip()
        aliases = tuple(alias.strip() for alias in self.aliases)
        if not ui_key or any(not alias for alias in aliases):
            raise ConfigurationError("provider service aliases must not be empty")
        if len(set(aliases)) != len(aliases) or any(
            alias in {service_id, ui_key} for alias in aliases
        ):
            raise ConfigurationError("provider service identifiers and aliases must be unique")
        if not isinstance(self.capabilities, ModelCapabilitySet):
            raise ConfigurationError("provider service capabilities must be canonical")
        protocol_capabilities = dict(self.protocol_capabilities)
        if not set(protocol_capabilities).issubset(supported):
            raise ConfigurationError(
                "provider service protocol capabilities use an unsupported protocol"
            )
        if any(
            not isinstance(value, ModelCapabilitySet) for value in protocol_capabilities.values()
        ):
            raise ConfigurationError("provider service protocol capabilities must be canonical")
        model_capabilities = dict(self.model_capabilities)
        if any(not model.strip() for model in model_capabilities):
            raise ConfigurationError("provider service model capability keys must be non-empty")
        if any(not isinstance(value, ModelCapabilitySet) for value in model_capabilities.values()):
            raise ConfigurationError("provider service model capabilities must be canonical")
        model_capabilities_by_protocol = {
            protocol: dict(values)
            for protocol, values in self.model_capabilities_by_protocol.items()
        }
        if not set(model_capabilities_by_protocol).issubset(supported):
            raise ConfigurationError(
                "provider service protocol model capabilities use an unsupported protocol"
            )
        if any(
            not isinstance(model, str)
            or not model.strip()
            or not isinstance(value, ModelCapabilitySet)
            for values in model_capabilities_by_protocol.values()
            for model, value in values.items()
        ):
            raise ConfigurationError(
                "provider service protocol model capabilities must be canonical"
            )
        object.__setattr__(self, "service_id", service_id)
        object.__setattr__(self, "display_name", self.display_name.strip())
        object.__setattr__(self, "default_base_url", _validate_default_url(self.default_base_url))
        object.__setattr__(self, "supported_protocols", supported)
        object.__setattr__(self, "dialect_by_protocol", MappingProxyType(dialect_by_protocol))
        object.__setattr__(self, "credential_style", credential_style)
        object.__setattr__(self, "model_catalog_strategy", catalog_strategy)
        object.__setattr__(self, "ui_key", ui_key)
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "protocol_capabilities", MappingProxyType(protocol_capabilities))
        object.__setattr__(self, "model_capabilities", MappingProxyType(model_capabilities))
        object.__setattr__(
            self,
            "model_capabilities_by_protocol",
            MappingProxyType(
                {
                    protocol: MappingProxyType(values)
                    for protocol, values in model_capabilities_by_protocol.items()
                }
            ),
        )

    def dialect_for(self, protocol: str | None = None) -> str:
        """Return the descriptor's dialect for a selected protocol."""

        selected = protocol or self.default_protocol
        try:
            return self.dialect_by_protocol[selected]
        except KeyError as error:
            raise ConfigurationError(
                f"provider service {self.service_id!r} does not support protocol {selected!r}"
            ) from error

    @property
    def catalog_strategy(self) -> ModelCatalogStrategy:
        """Compatibility alias for callers that use the shorter field name."""

        return ModelCatalogStrategy(self.model_catalog_strategy)

    def upstream_capabilities_for(
        self,
        *,
        protocol: str,
        model: str,
    ) -> ModelCapabilitySet:
        """Resolve only upstream service, protocol, and model capability facts."""

        resolved = self.capabilities
        protocol_capabilities = self.protocol_capabilities.get(protocol)
        if protocol_capabilities is not None:
            resolved = resolved.overlay(protocol_capabilities)
        model_capabilities = self.model_capabilities_by_protocol.get(protocol, {}).get(model)
        if model_capabilities is None:
            model_capabilities = self.model_capabilities.get(model)
        if model_capabilities is not None:
            resolved = resolved.overlay(model_capabilities)
        return resolved

    def capability_resolution_for(
        self,
        *,
        protocol: str,
        model: str,
        implementation: ModelCapabilitySet,
        configuration: ModelCapabilitySet | None = None,
    ) -> CapabilityResolution:
        """Resolve upstream facts against trusted adapter expressibility."""

        return resolve_capabilities(
            upstream=self.upstream_capabilities_for(protocol=protocol, model=model),
            implementation=implementation,
            configuration=configuration,
        )

    def capabilities_for(
        self,
        *,
        protocol: str,
        model: str,
        implementation: ModelCapabilitySet | None = None,
        configuration: ModelCapabilitySet | None = None,
    ) -> ModelCapabilitySet:
        """Return executable capabilities, failing closed without implementation evidence."""

        resolution = self.capability_resolution_for(
            protocol=protocol,
            model=model,
            implementation=implementation or ModelCapabilitySet.all_unknown(),
            configuration=configuration,
        )
        return resolution.effective


@dataclass(frozen=True, slots=True)
class ProviderServiceCatalog:
    """Immutable lookup catalog consumed by interfaces and configuration."""

    services: tuple[ProviderServiceDescriptor, ...]
    _by_identifier: Mapping[str, ProviderServiceDescriptor] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        services = tuple(self.services)
        if not services:
            raise ConfigurationError("provider service catalog must not be empty")
        if len({service.service_id for service in services}) != len(services):
            raise ConfigurationError("provider service ids must be unique")
        identifiers = [
            identifier for service in services for identifier in self._identifiers(service)
        ]
        if len(set(identifiers)) != len(identifiers):
            raise ConfigurationError("provider service catalog identifiers must be unique")
        object.__setattr__(self, "services", services)
        object.__setattr__(
            self,
            "_by_identifier",
            MappingProxyType(
                {
                    identifier: service
                    for service in services
                    for identifier in self._identifiers(service)
                }
            ),
        )

    @staticmethod
    def _identifiers(service: ProviderServiceDescriptor) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (service.service_id, service.ui_key or service.service_id, *service.aliases)
            )
        )

    def __iter__(self) -> Iterator[ProviderServiceDescriptor]:
        return iter(self.services)

    def get(self, identifier: str) -> ProviderServiceDescriptor | None:
        return self._by_identifier.get(identifier.strip())

    def require(self, identifier: str) -> ProviderServiceDescriptor:
        service = self.get(identifier)
        if service is None:
            raise ConfigurationError(f"unknown provider service: {identifier}")
        return service

    def match_profile(
        self,
        *,
        service_id: str | None,
        protocol: str,
        dialect: str,
        base_url: str,
    ) -> ProviderServiceDescriptor | None:
        """Match persisted profiles without guessing from model names alone."""

        if service_id:
            explicit = self.get(service_id)
            if explicit is not None:
                return explicit
        normalized_url = _normalized_url(base_url)
        default_protocol = tuple(
            service
            for service in self.services
            if service.default_protocol == protocol and service.dialect_for(protocol) == dialect
        )
        default_with_url = tuple(
            service
            for service in default_protocol
            if normalized_url and _normalized_url(service.default_base_url) == normalized_url
        )
        if default_with_url:
            return default_with_url[0]
        if default_protocol:
            return default_protocol[0]
        exact = tuple(
            service
            for service in self.services
            if protocol in service.supported_protocols
            and service.dialect_for(protocol) == dialect
            and normalized_url
            and _normalized_url(service.default_base_url) == normalized_url
        )
        if exact:
            return exact[0]
        compatible = tuple(
            service
            for service in self.services
            if protocol in service.supported_protocols and service.dialect_for(protocol) == dialect
        )
        if compatible:
            return compatible[0]
        return None

    def capabilities_for_profile(
        self,
        *,
        service_id: str | None,
        protocol: str,
        dialect: str,
        base_url: str,
        model: str,
        implementation: ModelCapabilitySet | None = None,
        configuration: ModelCapabilitySet | None = None,
    ) -> ModelCapabilitySet:
        """Resolve executable profile capabilities through the trusted seam."""

        return self.capability_resolution_for_profile(
            service_id=service_id,
            protocol=protocol,
            dialect=dialect,
            base_url=base_url,
            model=model,
            implementation=implementation,
            configuration=configuration,
        ).effective

    def upstream_capabilities_for_profile(
        self,
        *,
        service_id: str | None,
        protocol: str,
        dialect: str,
        base_url: str,
        model: str,
    ) -> ModelCapabilitySet:
        """Resolve only catalog/service/protocol/model facts for a profile."""

        service = self.match_profile(
            service_id=service_id,
            protocol=protocol,
            dialect=dialect,
            base_url=base_url,
        )
        if service is None:
            return ModelCapabilitySet.all_unknown()
        return service.upstream_capabilities_for(protocol=protocol, model=model)

    def capability_resolution_for_profile(
        self,
        *,
        service_id: str | None,
        protocol: str,
        dialect: str,
        base_url: str,
        model: str,
        implementation: ModelCapabilitySet | None = None,
        configuration: ModelCapabilitySet | None = None,
    ) -> CapabilityResolution:
        upstream = self.upstream_capabilities_for_profile(
            service_id=service_id,
            protocol=protocol,
            dialect=dialect,
            base_url=base_url,
            model=model,
        )
        return resolve_capabilities(
            upstream=upstream,
            implementation=implementation or ModelCapabilitySet.all_unknown(),
            configuration=configuration,
        )


def _descriptor(
    *,
    service_id: str,
    display_name: str,
    default_protocol: str,
    default_base_url: str,
    supported_protocols: frozenset[str],
    default_dialect: str,
    dialect_by_protocol: Mapping[str, str],
    catalog_strategy: ModelCatalogStrategy,
    ui_key: str,
    aliases: tuple[str, ...],
    label_key: str,
    model_placeholder_key: str,
    protocol_hint_key: str,
    publisher_id: str,
    capabilities: ModelCapabilitySet | None = None,
    model_capabilities: Mapping[str, ModelCapabilitySet] | None = None,
    model_capabilities_by_protocol: Mapping[str, Mapping[str, ModelCapabilitySet]] | None = None,
) -> ProviderServiceDescriptor:
    return ProviderServiceDescriptor(
        service_id=service_id,
        display_name=display_name,
        default_protocol=default_protocol,
        default_base_url=default_base_url,
        supported_protocols=supported_protocols,
        default_dialect=default_dialect,
        dialect_by_protocol=dialect_by_protocol,
        model_catalog_strategy=catalog_strategy,
        ui_key=ui_key,
        aliases=aliases,
        label_key=label_key,
        model_placeholder_key=model_placeholder_key,
        protocol_hint_key=protocol_hint_key,
        publisher=ProviderPublisherMetadata(publisher_id, display_name),
        capabilities=capabilities or ModelCapabilitySet.all_unknown(),
        model_capabilities=model_capabilities or {},
        model_capabilities_by_protocol=model_capabilities_by_protocol or {},
    )


_GEMINI_INTERACTIONS_SEARCH_MODELS = frozenset(
    {
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-flash-image-preview",
        "gemini-3.1-pro-preview",
        "gemini-3-pro-image-preview",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
    }
)
_GEMINI_INTERACTIONS_URL_CONTEXT_MODELS = frozenset(
    {
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    }
)
_GEMINI_INTERACTIONS_MIXED_TOOL_MODELS = frozenset(
    {
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
    }
)
_GEMINI_INTERACTIONS_MODELS = (
    _GEMINI_INTERACTIONS_SEARCH_MODELS
    | _GEMINI_INTERACTIONS_URL_CONTEXT_MODELS
    | _GEMINI_INTERACTIONS_MIXED_TOOL_MODELS
)


def _gemini_interactions_model_capabilities(model: str) -> ModelCapabilitySet:
    supported = {
        ModelCapability.FUNCTION_TOOLS,
        ModelCapability.VISION,
        ModelCapability.REASONING,
    }
    if model in _GEMINI_INTERACTIONS_SEARCH_MODELS:
        supported.add(ModelCapability.HOSTED_WEB_SEARCH)
    if model in _GEMINI_INTERACTIONS_URL_CONTEXT_MODELS:
        supported.add(ModelCapability.HOSTED_WEB_FETCH)
    if model in _GEMINI_INTERACTIONS_MIXED_TOOL_MODELS:
        supported.add(ModelCapability.MIXED_HOSTED_AND_CLIENT_TOOLS)
    return ModelCapabilitySet.from_supported(*supported)


DEFAULT_PROVIDER_SERVICE_CATALOG = ProviderServiceCatalog(
    (
        _descriptor(
            service_id="openai",
            display_name="OpenAI Responses",
            default_protocol="openai-responses",
            default_base_url="https://api.openai.com/v1",
            supported_protocols=frozenset({"openai-chat", "openai-responses"}),
            default_dialect="standard",
            dialect_by_protocol={"openai-chat": "standard", "openai-responses": "standard"},
            catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
            ui_key="openai",
            aliases=(),
            label_key="provider_settings.preset.openai",
            model_placeholder_key="provider_settings.model.openai",
            protocol_hint_key="provider_settings.protocol.openai",
            publisher_id="openai",
            capabilities=ModelCapabilitySet.from_supported(ModelCapability.HOSTED_WEB_SEARCH),
        ),
        _descriptor(
            service_id="generic-openai-compatible",
            display_name="Compatible Chat",
            default_protocol="openai-chat",
            default_base_url="",
            supported_protocols=frozenset({"openai-chat"}),
            default_dialect="standard",
            dialect_by_protocol={"openai-chat": "standard"},
            catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
            ui_key="compatible",
            aliases=("openai-compatible",),
            label_key="provider_settings.preset.compatible",
            model_placeholder_key="provider_settings.model.compatible",
            protocol_hint_key="provider_settings.protocol.compatible",
            publisher_id="generic-openai-compatible",
        ),
        _descriptor(
            service_id="deepseek",
            display_name="DeepSeek",
            default_protocol="openai-chat",
            default_base_url="https://api.deepseek.com",
            supported_protocols=frozenset({"openai-chat", "openai-responses"}),
            default_dialect="deepseek-v4",
            dialect_by_protocol={"openai-chat": "deepseek-v4", "openai-responses": "standard"},
            catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
            ui_key="deepseek",
            aliases=(),
            label_key="provider_settings.preset.deepseek",
            model_placeholder_key="provider_settings.model.deepseek",
            protocol_hint_key="provider_settings.protocol.deepseek",
            publisher_id="deepseek",
        ),
        _descriptor(
            service_id="anthropic",
            display_name="Anthropic",
            default_protocol="anthropic-messages",
            default_base_url="https://api.anthropic.com",
            supported_protocols=frozenset({"anthropic-messages"}),
            default_dialect="standard",
            dialect_by_protocol={"anthropic-messages": "standard"},
            catalog_strategy=ModelCatalogStrategy.ANTHROPIC,
            ui_key="anthropic",
            aliases=(),
            label_key="provider_settings.preset.anthropic",
            model_placeholder_key="provider_settings.model.anthropic",
            protocol_hint_key="provider_settings.protocol.anthropic",
            publisher_id="anthropic",
            model_capabilities={
                model: ModelCapabilitySet.from_supported(
                    ModelCapability.HOSTED_WEB_SEARCH,
                    ModelCapability.HOSTED_WEB_FETCH,
                )
                for model in (
                    "claude-fable-5",
                    "claude-opus-4-8",
                    "claude-mythos-5",
                    "claude-opus-4-7",
                    "claude-opus-4-6",
                    "claude-sonnet-5",
                    "claude-sonnet-4-6",
                )
            },
        ),
        _descriptor(
            service_id="google-ai-studio",
            display_name="Gemini",
            default_protocol="gemini-generate-content",
            default_base_url="https://generativelanguage.googleapis.com/v1beta",
            supported_protocols=frozenset({"gemini-generate-content", "gemini-interactions"}),
            default_dialect="standard",
            dialect_by_protocol={
                "gemini-generate-content": "standard",
                "gemini-interactions": "standard",
            },
            catalog_strategy=ModelCatalogStrategy.GEMINI,
            ui_key="gemini",
            aliases=(),
            label_key="provider_settings.preset.gemini",
            model_placeholder_key="provider_settings.model.gemini",
            protocol_hint_key="provider_settings.protocol.gemini",
            publisher_id="google-ai-studio",
            model_capabilities_by_protocol={
                "gemini-interactions": {
                    model: _gemini_interactions_model_capabilities(model)
                    for model in _GEMINI_INTERACTIONS_MODELS
                }
            },
        ),
        _descriptor(
            service_id="xai",
            display_name="xAI",
            default_protocol="openai-responses",
            default_base_url="https://api.x.ai/v1",
            supported_protocols=frozenset({"openai-responses"}),
            default_dialect="xai",
            dialect_by_protocol={"openai-responses": "xai"},
            catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
            ui_key="xai",
            aliases=(),
            label_key="provider_settings.preset.xai",
            model_placeholder_key="provider_settings.model.xai",
            protocol_hint_key="provider_settings.protocol.xai",
            publisher_id="xai",
            capabilities=ModelCapabilitySet.from_supported(
                ModelCapability.HOSTED_WEB_SEARCH,
                ModelCapability.HOSTED_X_SEARCH,
                ModelCapability.HOSTED_CODE_INTERPRETER,
            ),
        ),
    )
)


__all__ = [
    "DEFAULT_PROVIDER_SERVICE_CATALOG",
    "SUPPORTED_DIALECTS",
    "SUPPORTED_PROTOCOLS",
    "CredentialStyle",
    "ModelCatalogStrategy",
    "ProviderPublisherMetadata",
    "ProviderServiceCatalog",
    "ProviderServiceDescriptor",
]
