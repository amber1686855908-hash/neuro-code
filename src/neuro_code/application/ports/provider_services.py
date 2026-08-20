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
SUPPORTED_DIALECTS = frozenset(
    {
        "standard",
        "xai",
        "deepseek-v4",
        "kimi",
        "glm",
        "minimax",
    }
)
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


class ProtocolSupportStatus(StrEnum):
    """Evidence status for one model and one wire protocol."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderPublisherMetadata:
    """Optional publisher metadata; it has no runtime ownership."""

    publisher_id: str
    display_name: str

    def __post_init__(self) -> None:
        if not self.publisher_id.strip() or not self.display_name.strip():
            raise ConfigurationError("provider publisher metadata must be non-empty")


@dataclass(frozen=True, slots=True)
class ProviderEndpointVariant:
    """A non-secret inference endpoint variant owned by a service catalog.

    Region, workspace scope, and billing-plan labels are descriptive metadata.
    The selected base URL remains the profile's explicit route identity; no
    adapter is allowed to derive or rewrite it from these labels.
    """

    variant_id: str
    display_name: str
    base_url_by_protocol: Mapping[str, str] = field(default_factory=dict)
    region: str | None = None
    billing_plan: str | None = None
    workspace_scoped: bool = False
    usage_scope: str | None = None

    def __post_init__(self) -> None:
        variant_id = self.variant_id.strip()
        display_name = self.display_name.strip()
        if not variant_id or not display_name:
            raise ConfigurationError("provider endpoint variants must be non-empty")
        urls = {
            protocol: _validate_default_url(url)
            for protocol, url in self.base_url_by_protocol.items()
        }
        if not urls or any(
            protocol not in SUPPORTED_PROTOCOLS or not url for protocol, url in urls.items()
        ):
            raise ConfigurationError("provider endpoint variants must define supported HTTP URLs")
        for field_name, value in (
            ("region", self.region),
            ("billing_plan", self.billing_plan),
            ("usage_scope", self.usage_scope),
        ):
            if value is not None and not value.strip():
                raise ConfigurationError(f"provider endpoint {field_name} must not be empty")
        if not isinstance(self.workspace_scoped, bool):
            raise ConfigurationError("provider endpoint workspace_scoped must be a bool")
        object.__setattr__(self, "variant_id", variant_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "base_url_by_protocol", MappingProxyType(urls))
        object.__setattr__(
            self,
            "region",
            self.region.strip() if self.region is not None else None,
        )
        object.__setattr__(
            self,
            "billing_plan",
            self.billing_plan.strip() if self.billing_plan is not None else None,
        )
        object.__setattr__(
            self,
            "usage_scope",
            self.usage_scope.strip() if self.usage_scope is not None else None,
        )

    def base_url_for(self, protocol: str) -> str | None:
        """Return the endpoint for a selected protocol, if documented."""

        return self.base_url_by_protocol.get(protocol)


@dataclass(frozen=True, slots=True)
class ProviderModelDescriptor:
    """Versioned, conservative metadata for one service model identifier."""

    model_id: str
    runtime_model_id: str | None = None
    publisher: ProviderPublisherMetadata | None = None
    protocol_support: Mapping[str, ProtocolSupportStatus | str] = field(default_factory=dict)
    protocol_modes: Mapping[str, str] = field(default_factory=dict)
    capabilities: ModelCapabilitySet = field(default_factory=ModelCapabilitySet.all_unknown)
    capabilities_by_protocol: Mapping[str, ModelCapabilitySet] = field(default_factory=dict)

    def __post_init__(self) -> None:
        model_id = self.model_id.strip()
        if not model_id or len(model_id) > 512:
            raise ConfigurationError("provider model descriptor id is invalid")
        runtime_model_id = self.runtime_model_id
        if runtime_model_id is not None:
            runtime_model_id = runtime_model_id.strip()
            if not runtime_model_id:
                raise ConfigurationError("provider model runtime id must not be empty")
        try:
            protocol_support = {
                protocol: ProtocolSupportStatus(status)
                for protocol, status in self.protocol_support.items()
            }
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                "provider model protocol support metadata is invalid"
            ) from error
        if any(protocol not in SUPPORTED_PROTOCOLS for protocol in protocol_support):
            raise ConfigurationError("provider model protocol support uses an unsupported protocol")
        protocol_modes = dict(self.protocol_modes)
        if any(
            protocol not in SUPPORTED_PROTOCOLS or not isinstance(mode, str) or not mode.strip()
            for protocol, mode in protocol_modes.items()
        ):
            raise ConfigurationError("provider model protocol mode metadata is invalid")
        if not isinstance(self.capabilities, ModelCapabilitySet):
            raise ConfigurationError("provider model capabilities must be canonical")
        capabilities_by_protocol = dict(self.capabilities_by_protocol)
        if any(protocol not in SUPPORTED_PROTOCOLS for protocol in capabilities_by_protocol):
            raise ConfigurationError(
                "provider model capability metadata uses an unsupported protocol"
            )
        if any(
            not isinstance(capabilities, ModelCapabilitySet)
            for capabilities in capabilities_by_protocol.values()
        ):
            raise ConfigurationError("provider model capability metadata must be canonical")
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "runtime_model_id", runtime_model_id)
        object.__setattr__(self, "protocol_support", MappingProxyType(protocol_support))
        object.__setattr__(self, "protocol_modes", MappingProxyType(protocol_modes))
        object.__setattr__(
            self,
            "capabilities_by_protocol",
            MappingProxyType(capabilities_by_protocol),
        )

    def protocol_status_for(self, protocol: str) -> ProtocolSupportStatus:
        return ProtocolSupportStatus(
            self.protocol_support.get(protocol, ProtocolSupportStatus.UNKNOWN)
        )


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
    static_models: tuple[str, ...] = ()
    capabilities: ModelCapabilitySet = field(default_factory=ModelCapabilitySet.all_unknown)
    protocol_capabilities: Mapping[str, ModelCapabilitySet] = field(default_factory=dict)
    model_capabilities: Mapping[str, ModelCapabilitySet] = field(default_factory=dict)
    model_capabilities_by_protocol: Mapping[str, Mapping[str, ModelCapabilitySet]] = field(
        default_factory=dict
    )
    model_descriptors: Mapping[str, ProviderModelDescriptor] = field(default_factory=dict)
    catalog_strategy_by_protocol: Mapping[str, ModelCatalogStrategy | str] = field(
        default_factory=dict
    )
    endpoint_variants: tuple[ProviderEndpointVariant, ...] = ()
    protocol_hint_by_protocol: Mapping[str, str] = field(default_factory=dict)
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
        static_models = tuple(model.strip() for model in self.static_models)
        if any(
            not model
            or len(model) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in model)
            for model in static_models
        ):
            raise ConfigurationError("provider service static model identifiers are invalid")
        if len(set(static_models)) != len(static_models):
            raise ConfigurationError("provider service static model identifiers must be unique")
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
        model_descriptors = dict(self.model_descriptors)
        if any(
            not isinstance(model, str)
            or not model.strip()
            or not isinstance(descriptor, ProviderModelDescriptor)
            or descriptor.model_id != model
            for model, descriptor in model_descriptors.items()
        ):
            raise ConfigurationError("provider service model descriptors must be canonical")
        catalog_strategy_by_protocol = {}
        try:
            catalog_strategy_by_protocol = {
                protocol: ModelCatalogStrategy(strategy)
                for protocol, strategy in self.catalog_strategy_by_protocol.items()
            }
        except (TypeError, ValueError) as error:
            raise ConfigurationError("provider service catalog strategies are invalid") from error
        if not set(catalog_strategy_by_protocol).issubset(supported):
            raise ConfigurationError(
                "provider service catalog strategies use an unsupported protocol"
            )
        endpoint_variants = tuple(self.endpoint_variants)
        if any(not isinstance(variant, ProviderEndpointVariant) for variant in endpoint_variants):
            raise ConfigurationError("provider service endpoint variants must be canonical")
        if len({variant.variant_id for variant in endpoint_variants}) != len(endpoint_variants):
            raise ConfigurationError("provider service endpoint variant ids must be unique")
        protocol_hint_by_protocol = {
            protocol: hint.strip() for protocol, hint in self.protocol_hint_by_protocol.items()
        }
        if any(
            protocol not in supported or not hint
            for protocol, hint in protocol_hint_by_protocol.items()
        ):
            raise ConfigurationError("provider service protocol hints are invalid")
        object.__setattr__(self, "service_id", service_id)
        object.__setattr__(self, "display_name", self.display_name.strip())
        object.__setattr__(self, "default_base_url", _validate_default_url(self.default_base_url))
        object.__setattr__(self, "supported_protocols", supported)
        object.__setattr__(self, "dialect_by_protocol", MappingProxyType(dialect_by_protocol))
        object.__setattr__(self, "credential_style", credential_style)
        object.__setattr__(self, "model_catalog_strategy", catalog_strategy)
        object.__setattr__(self, "static_models", static_models)
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
        object.__setattr__(self, "model_descriptors", MappingProxyType(model_descriptors))
        object.__setattr__(
            self,
            "catalog_strategy_by_protocol",
            MappingProxyType(catalog_strategy_by_protocol),
        )
        object.__setattr__(self, "endpoint_variants", endpoint_variants)
        object.__setattr__(
            self,
            "protocol_hint_by_protocol",
            MappingProxyType(protocol_hint_by_protocol),
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

    def catalog_strategy_for(self, protocol: str | None = None) -> ModelCatalogStrategy:
        """Return the discovery strategy for a selected protocol."""

        selected = protocol or self.default_protocol
        if selected not in self.supported_protocols:
            raise ConfigurationError(
                f"provider service {self.service_id!r} does not support protocol {selected!r}"
            )
        return ModelCatalogStrategy(
            self.catalog_strategy_by_protocol.get(selected, self.catalog_strategy)
        )

    def protocol_hint_for(self, protocol: str | None = None) -> str | None:
        selected = protocol or self.default_protocol
        return self.protocol_hint_by_protocol.get(selected, self.protocol_hint_key)

    @property
    def default_endpoint_variant(self) -> ProviderEndpointVariant | None:
        return self.endpoint_variants[0] if self.endpoint_variants else None

    def endpoint_variant_for(self, variant_id: str) -> ProviderEndpointVariant | None:
        return next(
            (variant for variant in self.endpoint_variants if variant.variant_id == variant_id),
            None,
        )

    def endpoint_for(self, *, protocol: str, variant_id: str | None = None) -> str:
        """Return a catalog endpoint without hiding explicit profile overrides."""

        variant = (
            self.endpoint_variant_for(variant_id)
            if variant_id is not None
            else self.default_endpoint_variant
        )
        if variant is not None:
            endpoint = variant.base_url_for(protocol)
            if endpoint is not None:
                return endpoint
        return self.default_base_url

    def protocol_support_for(self, *, model: str, protocol: str) -> ProtocolSupportStatus:
        """Resolve model-specific protocol evidence without model-name guessing."""

        if protocol not in self.supported_protocols:
            return ProtocolSupportStatus.UNSUPPORTED
        if self.model_descriptors:
            descriptor = self.model_descriptors.get(model)
            return (
                descriptor.protocol_status_for(protocol)
                if descriptor is not None
                else ProtocolSupportStatus.UNKNOWN
            )
        return ProtocolSupportStatus.SUPPORTED

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
        descriptor = self.model_descriptors.get(model)
        if descriptor is not None:
            resolved = resolved.overlay(descriptor.capabilities)
            descriptor_protocol_capabilities = descriptor.capabilities_by_protocol.get(protocol)
            if descriptor_protocol_capabilities is not None:
                resolved = resolved.overlay(descriptor_protocol_capabilities)
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

    def protocol_support_for_profile(
        self,
        *,
        service_id: str | None,
        protocol: str,
        dialect: str,
        base_url: str,
        model: str,
    ) -> ProtocolSupportStatus:
        service = self.match_profile(
            service_id=service_id,
            protocol=protocol,
            dialect=dialect,
            base_url=base_url,
        )
        if service is None:
            return ProtocolSupportStatus.UNKNOWN
        return service.protocol_support_for(model=model, protocol=protocol)

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
    publisher_id: str | None,
    static_models: tuple[str, ...] = (),
    capabilities: ModelCapabilitySet | None = None,
    model_capabilities: Mapping[str, ModelCapabilitySet] | None = None,
    model_capabilities_by_protocol: Mapping[str, Mapping[str, ModelCapabilitySet]] | None = None,
    model_descriptors: Mapping[str, ProviderModelDescriptor] | None = None,
    catalog_strategy_by_protocol: Mapping[str, ModelCatalogStrategy | str] | None = None,
    endpoint_variants: tuple[ProviderEndpointVariant, ...] = (),
    protocol_hint_by_protocol: Mapping[str, str] | None = None,
    publisher: ProviderPublisherMetadata | None = None,
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
        static_models=static_models,
        ui_key=ui_key,
        aliases=aliases,
        label_key=label_key,
        model_placeholder_key=model_placeholder_key,
        protocol_hint_key=protocol_hint_key,
        publisher=publisher
        if publisher is not None
        else (ProviderPublisherMetadata(publisher_id, display_name) if publisher_id else None),
        capabilities=capabilities or ModelCapabilitySet.all_unknown(),
        model_capabilities=model_capabilities or {},
        model_capabilities_by_protocol=model_capabilities_by_protocol or {},
        model_descriptors=model_descriptors or {},
        catalog_strategy_by_protocol=catalog_strategy_by_protocol or {},
        endpoint_variants=endpoint_variants,
        protocol_hint_by_protocol=protocol_hint_by_protocol or {},
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


def _china_model_capabilities(*, vision: bool = False) -> ModelCapabilitySet:
    supported = {
        ModelCapability.FUNCTION_TOOLS,
        ModelCapability.PROMPT_CACHE,
        ModelCapability.REASONING,
    }
    if vision:
        supported.add(ModelCapability.VISION)
    return ModelCapabilitySet.from_supported(*supported)


_KIMI_CURRENT_MODELS = (
    "kimi-k3",
    "kimi-k2.7-code",
    "kimi-k2.7-code-highspeed",
    "kimi-k2.6",
    "kimi-k2.5",
)
_GLM_CURRENT_TEXT_MODELS = (
    "glm-5.3",
    "glm-5.2",
    "glm-5.1",
    "glm-5",
    "glm-5-turbo",
    "glm-4.7",
    "glm-4.6",
    "glm-4.5",
)
_MINIMAX_CURRENT_MODELS = (
    "MiniMax-M3",
    "MiniMax-M2.7",
    "MiniMax-M2.7-highspeed",
    "MiniMax-M2.5",
    "MiniMax-M2.5-highspeed",
    "MiniMax-M2.1",
    "MiniMax-M2.1-highspeed",
    "MiniMax-M2",
)


def _platform_capabilities(*, reasoning: bool = False, vision: bool = False) -> ModelCapabilitySet:
    supported = {ModelCapability.FUNCTION_TOOLS}
    if reasoning:
        supported.add(ModelCapability.REASONING)
    if vision:
        supported.add(ModelCapability.VISION)
    return ModelCapabilitySet.from_supported(*supported)


def _platform_model(
    model_id: str,
    *,
    protocols: Mapping[str, ProtocolSupportStatus],
    publisher: ProviderPublisherMetadata | None = None,
    protocol_modes: Mapping[str, str] | None = None,
    capabilities: ModelCapabilitySet | None = None,
    capabilities_by_protocol: Mapping[str, ModelCapabilitySet] | None = None,
    runtime_model_id: str | None = None,
) -> ProviderModelDescriptor:
    return ProviderModelDescriptor(
        model_id=model_id,
        runtime_model_id=runtime_model_id,
        publisher=publisher,
        protocol_support=protocols,
        protocol_modes=protocol_modes or {},
        capabilities=capabilities or _platform_capabilities(),
        capabilities_by_protocol=capabilities_by_protocol or {},
    )


def _publisher(publisher_id: str, display_name: str) -> ProviderPublisherMetadata:
    return ProviderPublisherMetadata(publisher_id, display_name)


_ARK_PROTOCOLS = {
    "openai-chat": ProtocolSupportStatus.SUPPORTED,
    "openai-responses": ProtocolSupportStatus.SUPPORTED,
    "anthropic-messages": ProtocolSupportStatus.UNSUPPORTED,
}
_ARK_MODEL_DESCRIPTORS = {
    model: _platform_model(
        model,
        protocols=_ARK_PROTOCOLS,
        publisher=_publisher("bytedance-doubao", "Doubao"),
        protocol_modes={"openai-chat": "native", "openai-responses": "native"},
        capabilities=_platform_capabilities(reasoning=True),
    )
    for model in ("doubao-seed-2-0-lite-260215", "doubao-seed-1-6-250615")
}

_QIANFAN_RESPONSES_MODELS = (
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "deepseek-v3.2",
    "deepseek-v3.2-think",
    "glm-5.1",
    "glm-5",
    "qwen3-coder-480b-a35b-instruct",
    "qwen3-235b-a22b-instruct-2507",
    "qwen3-14b",
    "qwen3-8b",
)
_QIANFAN_MODEL_DESCRIPTORS = {
    model: _platform_model(
        model,
        protocols={
            "openai-chat": ProtocolSupportStatus.SUPPORTED,
            "openai-responses": ProtocolSupportStatus.SUPPORTED,
            "anthropic-messages": ProtocolSupportStatus.SUPPORTED,
        },
        publisher=(
            _publisher("deepseek", "DeepSeek")
            if model.startswith("deepseek-")
            else _publisher("zhipu", "GLM")
            if model.startswith("glm-")
            else _publisher("qwen", "Qwen")
        ),
        protocol_modes={
            "openai-chat": "qianfan-openai-compatible",
            "openai-responses": "qianfan-responses",
            "anthropic-messages": "qianfan-anthropic-compatible",
        },
        capabilities=_platform_capabilities(
            reasoning="think" in model or model.startswith("deepseek-")
        ),
    )
    for model in _QIANFAN_RESPONSES_MODELS
}

_BAILIAN_QWEN_MODELS = (
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.6-flash",
    "qwen3.5-omni-plus",
    "qwen3-coder-next",
    "qwen3-coder-plus",
    "qwen3-coder-flash",
)
_BAILIAN_THIRD_PARTY_MODELS = (
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "kimi-k2.5",
    "kimi-k2-thinking",
    "glm-5.1",
    "glm-5",
    "glm-4.7",
    "glm-4.6",
    "MiniMax-M2.5",
    "MiniMax-M2.1",
)
_BAILIAN_MODEL_DESCRIPTORS = {
    **{
        model: _platform_model(
            model,
            protocols={
                "openai-chat": ProtocolSupportStatus.SUPPORTED,
                "openai-responses": ProtocolSupportStatus.SUPPORTED,
                "anthropic-messages": ProtocolSupportStatus.SUPPORTED,
            },
            publisher=_publisher("qwen", "Qwen"),
            protocol_modes={
                "openai-chat": "model-studio-openai-compatible",
                "openai-responses": "model-studio-responses",
                "anthropic-messages": "model-studio-anthropic-compatible",
            },
            capabilities=_platform_capabilities(
                reasoning=model not in {"qwen3.6-flash", "qwen3-coder-flash"},
                vision=model in {"qwen3.5-omni-plus"},
            ),
        )
        for model in _BAILIAN_QWEN_MODELS
    },
    **{
        model: _platform_model(
            model,
            protocols={
                "openai-chat": ProtocolSupportStatus.SUPPORTED,
                "openai-responses": ProtocolSupportStatus.UNKNOWN,
                "anthropic-messages": ProtocolSupportStatus.SUPPORTED,
            },
            publisher=(
                _publisher("deepseek", "DeepSeek")
                if model.startswith("deepseek-")
                else _publisher("moonshot", "Kimi")
                if model.startswith("kimi-")
                else _publisher("zhipu", "GLM")
                if model.startswith("glm-")
                else _publisher("minimax", "MiniMax")
            ),
            protocol_modes={
                "openai-chat": "model-studio-openai-compatible",
                "anthropic-messages": "model-studio-anthropic-compatible",
            },
            capabilities=_platform_capabilities(reasoning=True),
        )
        for model in _BAILIAN_THIRD_PARTY_MODELS
    },
}

_TOKENHUB_RESPONSES_NATIVE_MODELS = ("hy3", "hy3-preview")
_TOKENHUB_RESPONSES_CONVERTED_MODELS = (
    "glm-5.3",
    "glm-5.2",
    "glm-5.1",
    "kimi-k3",
    "kimi-k2.7-code",
    "kimi-k2.7-code-highspeed",
    "kimi-k2.6",
    "deepseek-v4-flash-202605",
    "deepseek-v4-pro-202606",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
)
_TOKENHUB_MODEL_DESCRIPTORS = {
    **{
        model: _platform_model(
            model,
            protocols={
                "openai-chat": ProtocolSupportStatus.SUPPORTED,
                "openai-responses": ProtocolSupportStatus.SUPPORTED,
                "anthropic-messages": ProtocolSupportStatus.SUPPORTED,
            },
            publisher=_publisher("tencent-hunyuan", "Tencent Hunyuan"),
            protocol_modes={
                "openai-chat": "tokenhub-openai-compatible",
                "openai-responses": "native",
                "anthropic-messages": "tokenhub-anthropic-compatible",
            },
            capabilities=_platform_capabilities(reasoning=True),
        )
        for model in _TOKENHUB_RESPONSES_NATIVE_MODELS
    },
    **{
        model: _platform_model(
            model,
            protocols={
                "openai-chat": ProtocolSupportStatus.SUPPORTED,
                "openai-responses": ProtocolSupportStatus.SUPPORTED,
                "anthropic-messages": ProtocolSupportStatus.SUPPORTED,
            },
            publisher=(
                _publisher("zhipu", "GLM")
                if model.startswith("glm-")
                else _publisher("moonshot", "Kimi")
                if model.startswith("kimi-")
                else _publisher("deepseek", "DeepSeek")
            ),
            protocol_modes={
                "openai-chat": "tokenhub-openai-compatible",
                "openai-responses": "compatibility-converted",
                "anthropic-messages": "tokenhub-anthropic-compatible",
            },
            capabilities=_platform_capabilities(reasoning=True),
        )
        for model in _TOKENHUB_RESPONSES_CONVERTED_MODELS
    },
    "hy-mt2-pro": _platform_model(
        "hy-mt2-pro",
        protocols={
            "openai-chat": ProtocolSupportStatus.SUPPORTED,
            "openai-responses": ProtocolSupportStatus.UNSUPPORTED,
            "anthropic-messages": ProtocolSupportStatus.SUPPORTED,
        },
        publisher=_publisher("tencent-hunyuan", "Tencent Hunyuan"),
        protocol_modes={
            "openai-chat": "tokenhub-openai-compatible",
            "anthropic-messages": "tokenhub-anthropic-compatible",
        },
        capabilities=_platform_capabilities(reasoning=True),
    ),
    "qwen3.5-plus": _platform_model(
        "qwen3.5-plus",
        protocols={
            "openai-chat": ProtocolSupportStatus.SUPPORTED,
            "openai-responses": ProtocolSupportStatus.UNKNOWN,
            "anthropic-messages": ProtocolSupportStatus.SUPPORTED,
        },
        publisher=_publisher("qwen", "Qwen"),
        protocol_modes={
            "openai-chat": "tokenhub-openai-compatible",
            "anthropic-messages": "tokenhub-anthropic-compatible",
        },
        capabilities=_platform_capabilities(reasoning=True, vision=True),
    ),
    "qwen3.5-flash": _platform_model(
        "qwen3.5-flash",
        protocols={
            "openai-chat": ProtocolSupportStatus.SUPPORTED,
            "openai-responses": ProtocolSupportStatus.UNKNOWN,
            "anthropic-messages": ProtocolSupportStatus.SUPPORTED,
        },
        publisher=_publisher("qwen", "Qwen"),
        protocol_modes={
            "openai-chat": "tokenhub-openai-compatible",
            "anthropic-messages": "tokenhub-anthropic-compatible",
        },
        capabilities=_platform_capabilities(reasoning=True, vision=True),
    ),
}


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
        _descriptor(
            service_id="kimi",
            display_name="Kimi",
            default_protocol="openai-chat",
            default_base_url="https://api.moonshot.ai/v1",
            supported_protocols=frozenset({"openai-chat"}),
            default_dialect="kimi",
            dialect_by_protocol={"openai-chat": "kimi"},
            catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
            static_models=("kimi-k3", "kimi-k2.7-code", "kimi-k2.6"),
            ui_key="kimi",
            aliases=("moonshot",),
            label_key="provider_settings.preset.kimi",
            model_placeholder_key="provider_settings.model.kimi",
            protocol_hint_key="provider_settings.protocol.kimi",
            publisher_id="moonshot",
            model_capabilities={
                model: _china_model_capabilities(vision=True) for model in _KIMI_CURRENT_MODELS
            },
        ),
        _descriptor(
            service_id="glm",
            display_name="GLM",
            default_protocol="openai-chat",
            default_base_url="https://open.bigmodel.cn/api/paas/v4",
            supported_protocols=frozenset({"openai-chat"}),
            default_dialect="glm",
            dialect_by_protocol={"openai-chat": "glm"},
            catalog_strategy=ModelCatalogStrategy.STATIC,
            static_models=(
                "glm-5.3",
                "glm-5.2",
                "glm-5.1",
                "glm-5",
                "glm-5-turbo",
                "glm-4.7",
                "glm-4.6",
                "glm-4.5",
                "glm-5v-turbo",
                "glm-4.6v",
            ),
            ui_key="glm",
            aliases=("zhipu",),
            label_key="provider_settings.preset.glm",
            model_placeholder_key="provider_settings.model.glm",
            protocol_hint_key="provider_settings.protocol.glm",
            publisher_id="zhipu",
            model_capabilities={
                **{model: _china_model_capabilities() for model in _GLM_CURRENT_TEXT_MODELS},
                "glm-5v-turbo": _china_model_capabilities(vision=True),
                "glm-4.6v": _china_model_capabilities(vision=True),
            },
        ),
        _descriptor(
            service_id="minimax",
            display_name="MiniMax",
            default_protocol="openai-chat",
            default_base_url="https://api.minimaxi.com/v1",
            supported_protocols=frozenset({"openai-chat"}),
            default_dialect="minimax",
            dialect_by_protocol={"openai-chat": "minimax"},
            catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
            static_models=("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5"),
            ui_key="minimax",
            aliases=(),
            label_key="provider_settings.preset.minimax",
            model_placeholder_key="provider_settings.model.minimax",
            protocol_hint_key="provider_settings.protocol.minimax",
            publisher_id="minimax",
            model_capabilities={
                **{model: _china_model_capabilities() for model in _MINIMAX_CURRENT_MODELS},
                "MiniMax-M3": _china_model_capabilities(vision=True),
            },
        ),
        _descriptor(
            service_id="ark",
            display_name="Volcengine Ark",
            default_protocol="openai-chat",
            default_base_url="https://ark.cn-beijing.volces.com/api/v3",
            supported_protocols=frozenset({"openai-chat", "openai-responses"}),
            default_dialect="standard",
            dialect_by_protocol={
                "openai-chat": "standard",
                "openai-responses": "standard",
            },
            catalog_strategy=ModelCatalogStrategy.STATIC,
            static_models=tuple(_ARK_MODEL_DESCRIPTORS),
            endpoint_variants=(
                ProviderEndpointVariant(
                    "cn-beijing",
                    "China (Beijing)",
                    {
                        "openai-chat": "https://ark.cn-beijing.volces.com/api/v3",
                        "openai-responses": "https://ark.cn-beijing.volces.com/api/v3",
                    },
                    region="cn-beijing",
                    billing_plan="account-or-plan",
                ),
            ),
            ui_key="ark",
            aliases=("volcengine-ark", "volcengine"),
            label_key="provider_settings.preset.ark",
            model_placeholder_key="provider_settings.model.ark",
            protocol_hint_key="provider_settings.protocol.ark",
            protocol_hint_by_protocol={
                "openai-chat": "provider_settings.protocol.ark.chat",
                "openai-responses": "provider_settings.protocol.ark.responses",
            },
            publisher_id=None,
            model_descriptors=_ARK_MODEL_DESCRIPTORS,
        ),
        _descriptor(
            service_id="qianfan",
            display_name="Baidu Qianfan",
            default_protocol="openai-chat",
            default_base_url="https://qianfan.baidubce.com/v2",
            supported_protocols=frozenset(
                {"openai-chat", "openai-responses", "anthropic-messages"}
            ),
            default_dialect="standard",
            dialect_by_protocol={
                "openai-chat": "standard",
                "openai-responses": "standard",
                "anthropic-messages": "standard",
            },
            catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
            catalog_strategy_by_protocol={
                "anthropic-messages": ModelCatalogStrategy.MANUAL_ONLY,
            },
            static_models=tuple(_QIANFAN_MODEL_DESCRIPTORS),
            endpoint_variants=(
                ProviderEndpointVariant(
                    "mainland",
                    "Baidu Qianfan · Mainland",
                    {
                        "openai-chat": "https://qianfan.baidubce.com/v2",
                        "openai-responses": "https://qianfan.baidubce.com/v2",
                        "anthropic-messages": "https://qianfan.baidubce.com/anthropic",
                    },
                    region="mainland",
                    billing_plan="account",
                ),
            ),
            ui_key="qianfan",
            aliases=("baidu", "baidu-qianfan"),
            label_key="provider_settings.preset.qianfan",
            model_placeholder_key="provider_settings.model.qianfan",
            protocol_hint_key="provider_settings.protocol.qianfan",
            protocol_hint_by_protocol={
                "openai-chat": "provider_settings.protocol.qianfan.chat",
                "openai-responses": "provider_settings.protocol.qianfan.responses",
                "anthropic-messages": "provider_settings.protocol.qianfan.anthropic",
            },
            publisher_id=None,
            model_descriptors=_QIANFAN_MODEL_DESCRIPTORS,
        ),
        _descriptor(
            service_id="bailian",
            display_name="Alibaba Model Studio",
            default_protocol="openai-chat",
            default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            supported_protocols=frozenset(
                {"openai-chat", "openai-responses", "anthropic-messages"}
            ),
            default_dialect="standard",
            dialect_by_protocol={
                "openai-chat": "standard",
                "openai-responses": "standard",
                "anthropic-messages": "standard",
            },
            catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
            catalog_strategy_by_protocol={
                "anthropic-messages": ModelCatalogStrategy.MANUAL_ONLY,
            },
            static_models=tuple(_BAILIAN_MODEL_DESCRIPTORS),
            endpoint_variants=(
                ProviderEndpointVariant(
                    "beijing-payg",
                    "Model Studio · Beijing · Pay-as-you-go",
                    {
                        "openai-chat": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "openai-responses": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "anthropic-messages": "https://dashscope.aliyuncs.com/apps/anthropic",
                    },
                    region="cn-beijing",
                    billing_plan="pay-as-you-go",
                ),
                ProviderEndpointVariant(
                    "singapore-workspace",
                    "Model Studio · Singapore · Workspace",
                    {
                        "openai-chat": "https://{workspace-id}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
                        "openai-responses": "https://{workspace-id}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
                        "anthropic-messages": "https://{workspace-id}.ap-southeast-1.maas.aliyuncs.com/apps/anthropic",
                    },
                    region="ap-southeast-1",
                    billing_plan="pay-as-you-go",
                    workspace_scoped=True,
                    usage_scope="workspace",
                ),
                ProviderEndpointVariant(
                    "singapore-payg",
                    "Model Studio · Singapore · Shared",
                    {
                        "openai-chat": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                        "openai-responses": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                        "anthropic-messages": "https://dashscope-intl.aliyuncs.com/apps/anthropic",
                    },
                    region="ap-southeast-1",
                    billing_plan="pay-as-you-go",
                ),
                ProviderEndpointVariant(
                    "us-virginia-payg",
                    "Model Studio · US (Virginia) · Pay-as-you-go",
                    {
                        "openai-chat": "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
                        "openai-responses": "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
                        "anthropic-messages": "https://dashscope-us.aliyuncs.com/apps/anthropic",
                    },
                    region="us-east-1",
                    billing_plan="pay-as-you-go",
                ),
            ),
            ui_key="bailian",
            aliases=("alibaba", "model-studio", "dashscope"),
            label_key="provider_settings.preset.bailian",
            model_placeholder_key="provider_settings.model.bailian",
            protocol_hint_key="provider_settings.protocol.bailian",
            protocol_hint_by_protocol={
                "openai-chat": "provider_settings.protocol.bailian.chat",
                "openai-responses": "provider_settings.protocol.bailian.responses",
                "anthropic-messages": "provider_settings.protocol.bailian.anthropic",
            },
            publisher_id=None,
            model_descriptors=_BAILIAN_MODEL_DESCRIPTORS,
        ),
        _descriptor(
            service_id="tokenhub",
            display_name="Tencent TokenHub",
            default_protocol="openai-chat",
            default_base_url="https://tokenhub.tencentmaas.com/v1",
            supported_protocols=frozenset(
                {"openai-chat", "openai-responses", "anthropic-messages"}
            ),
            default_dialect="standard",
            dialect_by_protocol={
                "openai-chat": "standard",
                "openai-responses": "standard",
                "anthropic-messages": "standard",
            },
            catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
            catalog_strategy_by_protocol={
                "anthropic-messages": ModelCatalogStrategy.ANTHROPIC,
            },
            static_models=tuple(_TOKENHUB_MODEL_DESCRIPTORS),
            endpoint_variants=(
                ProviderEndpointVariant(
                    "guangzhou",
                    "TokenHub · Guangzhou",
                    {
                        "openai-chat": "https://tokenhub.tencentmaas.com/v1",
                        "openai-responses": "https://tokenhub.tencentmaas.com/v1",
                        "anthropic-messages": "https://tokenhub.tencentmaas.com/v1",
                    },
                    region="ap-guangzhou",
                    billing_plan="account",
                ),
                ProviderEndpointVariant(
                    "singapore",
                    "TokenHub · Singapore",
                    {
                        "openai-chat": "https://tokenhub-intl.tencentmaas.com/v1",
                        "openai-responses": "https://tokenhub-intl.tencentmaas.com/v1",
                        "anthropic-messages": "https://tokenhub-intl.tencentmaas.com/v1",
                    },
                    region="ap-singapore",
                    billing_plan="account",
                ),
            ),
            ui_key="tokenhub",
            aliases=("tencent", "tencent-tokenhub"),
            label_key="provider_settings.preset.tokenhub",
            model_placeholder_key="provider_settings.model.tokenhub",
            protocol_hint_key="provider_settings.protocol.tokenhub",
            protocol_hint_by_protocol={
                "openai-chat": "provider_settings.protocol.tokenhub.chat",
                "openai-responses": "provider_settings.protocol.tokenhub.responses",
                "anthropic-messages": "provider_settings.protocol.tokenhub.anthropic",
            },
            publisher_id=None,
            model_descriptors=_TOKENHUB_MODEL_DESCRIPTORS,
        ),
    )
)


__all__ = [
    "DEFAULT_PROVIDER_SERVICE_CATALOG",
    "SUPPORTED_DIALECTS",
    "SUPPORTED_PROTOCOLS",
    "CredentialStyle",
    "ModelCatalogStrategy",
    "ProtocolSupportStatus",
    "ProviderEndpointVariant",
    "ProviderModelDescriptor",
    "ProviderPublisherMetadata",
    "ProviderServiceCatalog",
    "ProviderServiceDescriptor",
]
