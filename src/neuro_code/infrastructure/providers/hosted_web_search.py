"""Provider-hosted Web Search adapters.

Provider-hosted Web Search 基础设施适配器.

OpenAI and xAI share the streaming/lifecycle mechanics already owned by
``OpenAIResponsesProvider``; Anthropic uses its native Messages server-tool
adapter. This module owns only the bounded sidecar boundary, structured
extraction, and explicit WEB_SEARCH failover.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from neuro_code.application.ports.configuration import AppConfig, ProviderProfile
from neuro_code.application.ports.model import (
    CapabilityStatus,
    ModelCapability,
    ModelCapabilitySet,
    ModelProvider,
    ModelToolPolicy,
)
from neuro_code.application.ports.routing import ModelRoute, RuntimeRole
from neuro_code.application.ports.web_search import (
    HostedWebSearch,
    HostedWebSearchEvent,
    HostedWebSearchEventSink,
    WebSearchBackendResolver,
    WebSearchCitation,
    WebSearchError,
    WebSearchErrorCode,
    WebSearchRequest,
    WebSearchResult,
    WebSearchSource,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelTextDelta,
)
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.infrastructure.providers import create_provider
from neuro_code.infrastructure.providers.anthropic import AnthropicProvider
from neuro_code.infrastructure.providers.gemini_interactions import GeminiInteractionsProvider
from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider
from neuro_code.shared.errors import ConfigurationError, ProviderError

SIDE_CAR_SEARCH_SYSTEM_PROMPT = (
    "You are the web evidence backend for a coding agent. "
    "Search for evidence relevant to the query. "
    "Prefer: official documentation, official repositories, release notes/changelogs, "
    "specifications, maintainer issues/discussions. "
    "Use web fetch only when reading a selected source adds value. "
    "Return concise factual evidence. Preserve source attribution. "
    "Do not answer unrelated parts of the user's task. "
    "Do not propose code edits. Do not execute workspace actions."
)
_MAX_FAILURE_DETAIL = 500
_MAX_XAI_DOMAIN_FILTERS = 5
_MARKDOWN_CITATION = re.compile(r"\[\[(?P<number>\d+)\]\]\((?P<url>https?://[^)\s]+)\)")


def _bounded_prompt(request: WebSearchRequest) -> str:
    lines = [f"Query: {request.query}"]
    if request.allowed_domains:
        lines.append("Allowed domains: " + ", ".join(request.allowed_domains))
    if request.blocked_domains:
        lines.append("Blocked domains: " + ", ".join(request.blocked_domains))
    lines.append(f"Return at most {request.max_sources} distinct sources.")
    return "\n".join(lines)


def _response_output(response: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    output = response.get("output")
    if not isinstance(output, list):
        return ()
    return tuple(item for item in output if isinstance(item, Mapping))


def _response_text(response: Mapping[str, Any]) -> str:
    texts: list[str] = []
    for item in _response_output(response):
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, Mapping)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                texts.append(part["text"])
    return "\n".join(texts)


def _host(url: str) -> str | None:
    try:
        hostname = urlsplit(url).hostname
        return hostname.casefold() if hostname else None
    except ValueError:
        return None


def _matches_domain(host: str | None, domain: str) -> bool:
    return host == domain or (host is not None and host.endswith(f".{domain}"))


def _source_allowed(url: str, request: WebSearchRequest) -> bool:
    host = _host(url)
    if host is None:
        return False
    if request.allowed_domains and not any(
        _matches_domain(host, domain) for domain in request.allowed_domains
    ):
        return False
    return not request.blocked_domains or not any(
        _matches_domain(host, domain) for domain in request.blocked_domains
    )


def _string_value(value: object, *, maximum: int, default: str = "") -> str:
    return value.strip()[:maximum] if isinstance(value, str) else default


def _fallback_title(url: str) -> str:
    return _host(url) or "Web source"


def _source_from_mapping(
    raw: Mapping[str, Any],
    *,
    provider: str,
    request: WebSearchRequest,
) -> WebSearchSource | None:
    url = raw.get("url") or raw.get("link")
    if not isinstance(url, str) or not _source_allowed(url, request):
        return None
    title = _string_value(raw.get("title"), maximum=512) or _fallback_title(url)
    snippet = (
        _string_value(
            raw.get("snippet") or raw.get("description") or raw.get("text"),
            maximum=2_000,
        )
        or None
    )
    try:
        return WebSearchSource(url, title, provider, snippet)
    except (TypeError, ValueError):
        return None


def _source_from_url(
    url: str,
    *,
    provider: str,
    request: WebSearchRequest,
) -> WebSearchSource | None:
    if not _source_allowed(url, request):
        return None
    try:
        return WebSearchSource(url, _fallback_title(url), provider)
    except (TypeError, ValueError):
        return None


def _citation_from_mapping(
    raw: Mapping[str, Any],
    *,
    response_text: str,
    request: WebSearchRequest,
) -> WebSearchCitation | None:
    payload: Mapping[str, Any] = raw
    for key in ("url_citation", "url_citation_preview"):
        nested = raw.get(key)
        if isinstance(nested, Mapping):
            payload = nested
            break
    url = payload.get("url")
    if not isinstance(url, str) or not _source_allowed(url, request):
        return None
    title = _string_value(payload.get("title"), maximum=512) or _fallback_title(url)
    start = payload.get("start_index", payload.get("start"))
    end = payload.get("end_index", payload.get("end"))
    cited_text: str | None = None
    if (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
    ):
        if 0 <= start <= end <= len(response_text):
            cited_text = response_text[start:end]
        else:
            start = None
            end = None
    else:
        start = None
        end = None
    try:
        return WebSearchCitation(url, title, cited_text, start, end)
    except (TypeError, ValueError):
        return None


def extract_web_search_evidence(
    response: Mapping[str, Any],
    *,
    provider: str,
    request: WebSearchRequest,
) -> tuple[str, tuple[WebSearchSource, ...], tuple[WebSearchCitation, ...], bool]:
    """Extract only structured source/citation fields from a Responses result."""

    text = _response_text(response)
    sources: list[WebSearchSource] = []
    citations: list[WebSearchCitation] = []
    truncated = False

    def add_source(raw: object) -> None:
        nonlocal truncated
        if isinstance(raw, str):
            source = _source_from_url(raw, provider=provider, request=request)
        elif isinstance(raw, Mapping):
            source = _source_from_mapping(raw, provider=provider, request=request)
        else:
            return
        if source is None:
            return
        if any(item.url.casefold() == source.url.casefold() for item in sources):
            return
        if len(sources) >= request.max_sources:
            truncated = True
            return
        sources.append(source)

    def add_citation(raw: object) -> None:
        if not isinstance(raw, Mapping):
            return
        citation = _citation_from_mapping(raw, response_text=text, request=request)
        if citation is None:
            return
        if any(
            item.url.casefold() == citation.url.casefold()
            and item.start == citation.start
            and item.end == citation.end
            for item in citations
        ):
            return
        if len(citations) >= request.max_sources:
            return
        citations.append(citation)

    for item in _response_output(response):
        if item.get("type") == "web_search_call":
            action = item.get("action")
            if isinstance(action, Mapping):
                raw_sources = action.get("sources")
                if isinstance(raw_sources, list):
                    for raw_source in raw_sources:
                        add_source(raw_source)
            raw_sources = item.get("sources")
            if isinstance(raw_sources, list):
                for raw_source in raw_sources:
                    add_source(raw_source)
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                annotations = part.get("annotations")
                if isinstance(annotations, list):
                    for annotation in annotations:
                        if isinstance(annotation, Mapping) and annotation.get("type") in {
                            "url_citation",
                            "url_citation_preview",
                        }:
                            add_citation(annotation)

    for key in ("sources", "results"):
        raw_sources = response.get(key)
        if isinstance(raw_sources, list):
            for raw_source in raw_sources:
                add_source(raw_source)
    raw_citations = response.get("citations")
    if isinstance(raw_citations, list):
        for raw_citation in raw_citations:
            if isinstance(raw_citation, str):
                # xAI exposes its complete citation list as URL strings on the
                # Responses result; retain those URLs as sources even though
                # they have no positional inline span.
                add_source(raw_citation)
            else:
                add_citation(raw_citation)

    # xAI Responses may preserve inline citations as Markdown even when a
    # compatibility gateway omits structured annotations.  This is a bounded
    # fallback only; structured annotations and search-call sources above win.
    for match in _MARKDOWN_CITATION.finditer(text):
        add_citation(
            {
                "url": match.group("url"),
                "title": f"Citation {match.group('number')}",
                "start_index": match.start(),
                "end_index": match.end(),
            }
        )

    return text, tuple(sources), tuple(citations), truncated


def _has_completed_web_search_execution(response: Mapping[str, Any]) -> bool:
    """Require provider-side execution evidence, never just response text."""

    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "web_search_call":
                continue
            status = item.get("status")
            if status is None or status == "completed":
                return True

    usage = response.get("server_side_tool_usage")
    if isinstance(usage, Mapping):
        for name, count in usage.items():
            if not isinstance(name, str):
                continue
            normalized = name.casefold()
            if "web_search" not in normalized and normalized not in {
                "browse_page",
                "open_page",
                "open_page_with_find",
            }:
                continue
            if isinstance(count, bool):
                if count:
                    return True
            elif isinstance(count, (int, float)) and count > 0:
                return True
    citations = response.get("citations")
    if isinstance(citations, list):
        for citation in citations:
            url = citation if isinstance(citation, str) else None
            if isinstance(citation, Mapping):
                raw_url = citation.get("url") or citation.get("link")
                url = raw_url if isinstance(raw_url, str) else None
            if url is None:
                continue
            try:
                parsed = urlsplit(url.strip())
            except ValueError:
                continue
            if parsed.scheme.casefold() in {"http", "https"} and parsed.hostname:
                return True
    return False


def _anthropic_content(response: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    content = response.get("content")
    if not isinstance(content, list):
        return ()
    return tuple(item for item in content if isinstance(item, Mapping))


def _anthropic_result_content(block: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    content = block.get("content")
    if isinstance(content, Mapping):
        return (content,)
    if isinstance(content, list):
        return tuple(item for item in content if isinstance(item, Mapping))
    return ()


def _anthropic_server_error_code(block: Mapping[str, Any]) -> str | None:
    error_code = block.get("error_code")
    if isinstance(error_code, str):
        return error_code
    content = block.get("content")
    content_type = content.get("type") if isinstance(content, Mapping) else None
    if isinstance(content, Mapping) and (
        (
            isinstance(content_type, str)
            and content_type in {"web_search_tool_result_error", "web_fetch_tool_result_error"}
        )
        or isinstance(content.get("error_code"), str)
    ):
        code = content.get("error_code")
        return code if isinstance(code, str) else "server_tool_error"
    return None


def _anthropic_response_text(responses: Sequence[Mapping[str, Any]]) -> str:
    texts: list[str] = []
    for response in responses:
        for block in _anthropic_content(response):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
    return "\n".join(texts)


def _anthropic_server_result_items(
    responses: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    results: list[tuple[str, Mapping[str, Any]]] = []
    for response in responses:
        for block in _anthropic_content(response):
            block_type = block.get("type")
            name = _SERVER_RESULT_NAMES.get(block_type if isinstance(block_type, str) else "")
            if name is None:
                continue
            if _anthropic_server_error_code(block) is not None:
                continue
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str) and tool_use_id:
                results.append((name, block))
    return tuple(results)


_SERVER_RESULT_NAMES = {
    "web_search_tool_result": "web_search",
    "web_fetch_tool_result": "web_fetch",
}


def _has_completed_anthropic_web_search_execution(
    responses: Sequence[Mapping[str, Any]],
) -> bool:
    """Require a paired Anthropic server call and successful result block."""

    started: set[str] = set()
    for response in responses:
        for block in _anthropic_content(response):
            if block.get("type") != "server_tool_use" or block.get("name") != "web_search":
                continue
            identifier = block.get("id")
            if isinstance(identifier, str) and identifier:
                started.add(identifier)
    for name, block in _anthropic_server_result_items(responses):
        if name != "web_search":
            continue
        identifier = block.get("tool_use_id")
        if isinstance(identifier, str) and identifier in started:
            return True
    return False


def extract_anthropic_web_search_evidence(
    responses: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    provider: str,
    request: WebSearchRequest,
) -> tuple[str, tuple[WebSearchSource, ...], tuple[WebSearchCitation, ...], bool]:
    """Extract bounded sources/citations from Anthropic native result blocks."""

    normalized = (responses,) if isinstance(responses, Mapping) else tuple(responses)
    text = _anthropic_response_text(normalized)
    sources: list[WebSearchSource] = []
    citations: list[WebSearchCitation] = []
    truncated = False
    fetch_documents: list[tuple[str, str]] = []

    def add_source(raw: object, *, title: object = None, snippet: object = None) -> None:
        nonlocal truncated
        if not isinstance(raw, str):
            return
        source = _source_from_mapping(
            {
                "url": raw,
                "title": title,
                "snippet": snippet,
            },
            provider=provider,
            request=request,
        )
        if source is None:
            return
        if any(item.url.casefold() == source.url.casefold() for item in sources):
            return
        if len(sources) >= request.max_sources:
            truncated = True
            return
        sources.append(source)

    def add_citation(
        url: object,
        *,
        title: object = None,
        cited_text: object = None,
        start: object = None,
        end: object = None,
    ) -> None:
        if not isinstance(url, str) or not _source_allowed(url, request):
            return
        title_value = _string_value(title, maximum=512) or _fallback_title(url)
        cited = _string_value(cited_text, maximum=4_000) if isinstance(cited_text, str) else None
        normalized_start: int | None = None
        normalized_end: int | None = None
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start <= end <= len(text)
        ):
            normalized_start = start
            normalized_end = end
            if cited is None:
                cited = text[start:end]
        try:
            citation = WebSearchCitation(
                url,
                title_value,
                cited,
                normalized_start,
                normalized_end,
            )
        except (TypeError, ValueError):
            return
        if any(
            item.url.casefold() == citation.url.casefold()
            and item.start == citation.start
            and item.end == citation.end
            and item.cited_text == citation.cited_text
            for item in citations
        ):
            return
        if len(citations) < request.max_sources:
            citations.append(citation)

    # Fetch character citations refer to the document order in the native fetch
    # result. Collect that index before walking text blocks so content ordering
    # cannot make a valid citation disappear.
    for response in normalized:
        for block in _anthropic_content(response):
            if block.get("type") != "web_fetch_tool_result":
                continue
            for raw_result in _anthropic_result_content(block):
                if (
                    not isinstance(raw_result, Mapping)
                    or raw_result.get("type") != "web_fetch_result"
                ):
                    continue
                document = raw_result.get("content") or raw_result.get("document")
                result_url = raw_result.get("url")
                if not isinstance(result_url, str) and isinstance(document, Mapping):
                    result_url = document.get("source")
                result_title: object = raw_result.get("title")
                if isinstance(document, Mapping):
                    result_title = document.get("title") or result_title
                if isinstance(result_url, str) and _source_allowed(result_url, request):
                    fetch_documents.append((result_url, _string_value(result_title, maximum=512)))

    for response in normalized:
        for block in _anthropic_content(response):
            block_type = block.get("type")
            if block_type == "text":
                raw_citations = block.get("citations")
                if not isinstance(raw_citations, list):
                    continue
                for raw in raw_citations:
                    if not isinstance(raw, Mapping):
                        continue
                    citation_type = raw.get("type")
                    if citation_type == "web_search_result_location":
                        add_source(
                            raw.get("url"),
                            title=raw.get("title"),
                            snippet=raw.get("cited_text"),
                        )
                        add_citation(
                            raw.get("url"),
                            title=raw.get("title"),
                            cited_text=raw.get("cited_text"),
                        )
                    elif citation_type == "char_location":
                        document_index = raw.get("document_index")
                        if (
                            isinstance(document_index, int)
                            and not isinstance(document_index, bool)
                            and 0 <= document_index < len(fetch_documents)
                        ):
                            url, fetch_title = fetch_documents[document_index]
                            add_citation(
                                url,
                                title=raw.get("title") or fetch_title,
                                cited_text=raw.get("cited_text"),
                                start=raw.get("start_char_index"),
                                end=raw.get("end_char_index"),
                            )
                continue
            if block_type not in {"web_search_tool_result", "web_fetch_tool_result"}:
                continue
            for raw_result in _anthropic_result_content(block):
                result_type = raw_result.get("type")
                if result_type == "web_search_result":
                    add_source(
                        raw_result.get("url"),
                        title=raw_result.get("title"),
                        snippet=raw_result.get("snippet") or raw_result.get("description"),
                    )
                elif result_type == "web_fetch_result":
                    result_url2: object = raw_result.get("url")
                    document = raw_result.get("content") or raw_result.get("document")
                    result_title2: object = raw_result.get("title")
                    if isinstance(document, Mapping):
                        result_title2 = document.get("title") or result_title2
                        if not isinstance(result_url2, str):
                            result_url2 = document.get("source")
                    if isinstance(result_url2, str):
                        add_source(result_url2, title=result_title2)

    return text, tuple(sources), tuple(citations), truncated


def _gemini_steps(response: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw_steps = response.get("steps")
    if not isinstance(raw_steps, list):
        return ()
    return tuple(step for step in raw_steps if isinstance(step, Mapping))


def _gemini_response_text(response: Mapping[str, Any]) -> str:
    texts: list[str] = []
    for step in _gemini_steps(response):
        if step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, Mapping)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                texts.append(block["text"])
    return "".join(texts)


def _has_completed_gemini_google_search_execution(
    response: Mapping[str, Any],
) -> bool:
    """Require a Google Search call followed by a non-error result step."""

    calls: set[str] = set()
    for step in _gemini_steps(response):
        if step.get("type") != "google_search_call":
            continue
        identifier = step.get("id") or step.get("call_id") or f"search-{len(calls)}"
        if isinstance(identifier, str) and identifier:
            calls.add(identifier)
    if not calls:
        return False
    for step in _gemini_steps(response):
        if step.get("type") != "google_search_result" or _is_gemini_error_step(step):
            continue
        identifier = step.get("call_id") or step.get("id")
        if not isinstance(identifier, str) or not identifier:
            if len(calls) != 1:
                continue
            identifier = next(iter(calls))
        if identifier not in calls:
            continue
        status = step.get("status")
        if isinstance(status, str) and status.casefold() not in {
            "success",
            "succeeded",
            "completed",
            "complete",
            "ok",
        }:
            continue
        if "result" not in step and status is None:
            continue
        return True
    return False


def _is_gemini_error_step(step: Mapping[str, Any]) -> bool:
    if step.get("is_error") is True or isinstance(step.get("error"), Mapping):
        return True
    status = step.get("status")
    return isinstance(status, str) and status.casefold() in {
        "failed",
        "error",
        "errored",
        "cancelled",
        "canceled",
        "unsafe",
        "blocked",
    }


def extract_gemini_web_search_evidence(
    response: Mapping[str, Any],
    *,
    provider: str,
    request: WebSearchRequest,
) -> tuple[str, tuple[WebSearchSource, ...], tuple[WebSearchCitation, ...], bool]:
    """Extract only URL citations from Gemini model-output annotations.

    Google Search's ``search_suggestions`` field is provider-rendered HTML and
    is intentionally ignored.  The model-output ``url_citation`` annotations
    are the portable evidence boundary.
    """

    text = _gemini_response_text(response)
    sources: list[WebSearchSource] = []
    citations: list[WebSearchCitation] = []
    truncated = False

    def add_source(annotation: Mapping[str, Any]) -> None:
        nonlocal truncated
        url = annotation.get("url")
        if not isinstance(url, str) or not _source_allowed(url, request):
            return
        title = _string_value(annotation.get("title"), maximum=512) or _fallback_title(url)
        cited_text = _string_value(annotation.get("cited_text"), maximum=2_000) or None
        try:
            source = WebSearchSource(url, title, provider, cited_text)
        except (TypeError, ValueError):
            return
        if any(item.url.casefold() == url.casefold() for item in sources):
            return
        if len(sources) >= request.max_sources:
            truncated = True
            return
        sources.append(source)

    def add_citation(annotation: Mapping[str, Any]) -> None:
        url = annotation.get("url")
        if not isinstance(url, str) or not _source_allowed(url, request):
            return
        title = _string_value(annotation.get("title"), maximum=512) or _fallback_title(url)
        start = annotation.get("start_index")
        end = annotation.get("end_index")
        cited_text: str | None = None
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start <= end <= len(text)
        ):
            cited_text = text[start:end]
        else:
            start = None
            end = None
        try:
            citation = WebSearchCitation(url, title, cited_text, start, end)
        except (TypeError, ValueError):
            return
        if any(
            item.url.casefold() == url.casefold()
            and item.start == citation.start
            and item.end == citation.end
            for item in citations
        ):
            return
        if len(citations) >= request.max_sources:
            return
        citations.append(citation)

    for step in _gemini_steps(response):
        if step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            annotations = block.get("annotations")
            if not isinstance(annotations, list):
                continue
            for annotation in annotations:
                if isinstance(annotation, Mapping) and annotation.get("type") == "url_citation":
                    add_source(annotation)
                    add_citation(annotation)
    return text, tuple(sources), tuple(citations), truncated


def _provider_tool_options(
    profile: ProviderProfile,
    request: WebSearchRequest,
) -> Mapping[str, Mapping[str, object]]:
    """Map canonical filters to the selected provider's server-tool schema."""

    domains = request.allowed_domains or request.blocked_domains
    if profile.protocol == "anthropic-messages":
        options: dict[str, object] = {"max_uses": 1}
        if request.allowed_domains:
            options["allowed_domains"] = list(request.allowed_domains)
        elif request.blocked_domains:
            options["blocked_domains"] = list(request.blocked_domains)
        return {"web_search": options}
    if profile.protocol == "gemini-interactions":
        if domains:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNSUPPORTED,
                "Gemini Google Search does not expose portable domain filters",
            )
        return {}
    if not domains:
        return {}
    if profile.dialect == "xai" and len(domains) > _MAX_XAI_DOMAIN_FILTERS:
        raise WebSearchError(
            WebSearchErrorCode.SEARCH_INVALID_REQUEST,
            "xAI hosted web search accepts at most 5 domain filters",
        )
    if request.allowed_domains:
        filter_name = "allowed_domains"
    else:
        filter_name = "excluded_domains" if profile.dialect == "xai" else "blocked_domains"
    return {"web_search": {"filters": {filter_name: list(domains)}}}


class _ResponseCapture:
    def __init__(self) -> None:
        self.response: Mapping[str, Any] | None = None
        self.responses: list[Mapping[str, Any]] = []

    def observe(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.responses.append(response)


class ResponsesHostedWebSearchBackend:
    """One OpenAI or xAI Responses profile used as a sidecar search backend."""

    def __init__(
        self,
        profile: ProviderProfile,
        provider_factory: Callable[
            [Callable[[Mapping[str, Any]], None], WebSearchRequest], ModelProvider
        ],
    ) -> None:
        self._profile = profile
        self._provider_factory = provider_factory
        self._capabilities = profile.effective_capabilities(
            OpenAIResponsesProvider.implementation_capabilities(
                dialect=profile.dialect,
                builtin_tools=profile.builtin_tools,
            )
        )

    @property
    def provider_profile(self) -> str:
        return self._profile.name

    @property
    def model(self) -> str:
        return self._profile.model

    @property
    def capabilities(self) -> ModelCapabilitySet:
        return self._capabilities

    @staticmethod
    def _normalize_error(error: BaseException) -> WebSearchError:
        detail = " ".join(str(error).split())[:_MAX_FAILURE_DETAIL]
        lowered = detail.casefold()
        if (
            (
                isinstance(error, ConfigurationError)
                and any(word in lowered for word in ("credential", "api key", "authentication"))
            )
            or "401" in lowered
            or "403" in lowered
            or "unauthorized" in lowered
        ):
            code = WebSearchErrorCode.SEARCH_AUTHENTICATION
        elif (
            "429" in lowered
            or "rate limit" in lowered
            or "too many" in lowered
            or "too_many" in lowered
        ):
            code = WebSearchErrorCode.SEARCH_RATE_LIMIT
        elif any(
            marker in lowered
            for marker in (
                "invalid_tool_input",
                "query_too_long",
                "request_too_large",
                "max_uses_exceeded",
            )
        ):
            code = WebSearchErrorCode.SEARCH_INVALID_REQUEST
        elif "timeout" in lowered or "timed out" in lowered:
            code = WebSearchErrorCode.SEARCH_TIMEOUT
        elif "unsupported" in lowered or "not support" in lowered:
            code = WebSearchErrorCode.SEARCH_UNSUPPORTED
        elif "400" in lowered or "invalid" in lowered or "bad request" in lowered:
            code = WebSearchErrorCode.SEARCH_INVALID_REQUEST
        else:
            code = WebSearchErrorCode.SEARCH_PROVIDER_ERROR
        return WebSearchError(code, detail or "hosted search provider failed")

    async def search(
        self,
        request: WebSearchRequest,
        *,
        event_sink: HostedWebSearchEventSink | None = None,
    ) -> WebSearchResult:
        if (
            self._capabilities.status(ModelCapability.HOSTED_WEB_SEARCH)
            is not CapabilityStatus.SUPPORTED
        ):
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNSUPPORTED,
                "hosted web search is not explicitly supported by this profile",
            )
        capture = _ResponseCapture()
        try:
            provider = self._provider_factory(capture.observe, request)
            context = ModelContext(
                (
                    Message(Role.SYSTEM, SIDE_CAR_SEARCH_SYSTEM_PROMPT),
                    Message(Role.USER, _bounded_prompt(request)),
                )
            )
            completion: ModelCompleted | None = None
            streamed_text = ""
            async for event in provider.stream(
                context,
                (),
                tool_policy=ModelToolPolicy.ALLOWED,
            ):
                if isinstance(event, (ModelBackendToolStarted, ModelBackendToolCompleted)):
                    if event_sink is not None:
                        await event_sink(
                            HostedWebSearchEvent(
                                self.provider_profile,
                                self.model,
                                event.call_id,
                                event.name,
                                isinstance(event, ModelBackendToolCompleted),
                            )
                        )
                elif isinstance(event, ModelTextDelta):
                    streamed_text += event.text
                elif isinstance(event, ModelCompleted):
                    completion = event
            if completion is None:
                raise ProviderError("hosted search stream ended without completion")
            if capture.response is None:
                raise ProviderError("hosted search response did not expose structured evidence")
            if not _has_completed_web_search_execution(capture.response):
                raise WebSearchError(
                    WebSearchErrorCode.SEARCH_PROVIDER_DID_NOT_SEARCH,
                    "hosted search provider returned no completed web_search_call",
                )
            evidence_text, sources, citations, truncated = extract_web_search_evidence(
                capture.response,
                provider=self.provider_profile,
                request=request,
            )
            if not evidence_text:
                evidence_text = completion.response_text or streamed_text
            return WebSearchResult(
                query=request.query,
                evidence_text=evidence_text,
                sources=sources,
                citations=citations,
                provider_profile=self.provider_profile,
                model=self.model,
                truncated=truncated,
                metadata={"auxiliary": True, "source_count": len(sources)},
            )
        except asyncio.CancelledError:
            raise
        except WebSearchError:
            raise
        except (ConfigurationError, ProviderError, OSError, UnicodeError) as error:
            raise self._normalize_error(error) from error


class AnthropicHostedWebSearchBackend(ResponsesHostedWebSearchBackend):
    """One Anthropic Messages profile used as a hosted-search sidecar."""

    def __init__(
        self,
        profile: ProviderProfile,
        provider_factory: Callable[
            [Callable[[Mapping[str, Any]], None], WebSearchRequest], ModelProvider
        ],
    ) -> None:
        self._profile = profile
        self._provider_factory = provider_factory
        self._capabilities = profile.effective_capabilities(
            AnthropicProvider.implementation_capabilities(
                model=profile.model,
                builtin_tools=profile.builtin_tools,
            )
        )

    async def search(
        self,
        request: WebSearchRequest,
        *,
        event_sink: HostedWebSearchEventSink | None = None,
    ) -> WebSearchResult:
        if (
            self._capabilities.status(ModelCapability.HOSTED_WEB_SEARCH)
            is not CapabilityStatus.SUPPORTED
        ):
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNSUPPORTED,
                "hosted web search is not explicitly supported by this profile",
            )
        capture = _ResponseCapture()
        try:
            provider = self._provider_factory(capture.observe, request)
            context = ModelContext(
                (
                    Message(Role.SYSTEM, SIDE_CAR_SEARCH_SYSTEM_PROMPT),
                    Message(Role.USER, _bounded_prompt(request)),
                )
            )
            completion: ModelCompleted | None = None
            streamed_text = ""
            async for event in provider.stream(
                context,
                (),
                tool_policy=ModelToolPolicy.ALLOWED,
            ):
                if isinstance(event, (ModelBackendToolStarted, ModelBackendToolCompleted)):
                    if event_sink is not None:
                        await event_sink(
                            HostedWebSearchEvent(
                                self.provider_profile,
                                self.model,
                                event.call_id,
                                event.name,
                                isinstance(event, ModelBackendToolCompleted),
                            )
                        )
                elif isinstance(event, ModelTextDelta):
                    streamed_text += event.text
                elif isinstance(event, ModelCompleted):
                    completion = event
            if completion is None:
                raise ProviderError("hosted search stream ended without completion")
            if not capture.responses:
                raise ProviderError("hosted search response did not expose structured evidence")
            if not _has_completed_anthropic_web_search_execution(capture.responses):
                raise WebSearchError(
                    WebSearchErrorCode.SEARCH_PROVIDER_DID_NOT_SEARCH,
                    "hosted search provider returned no completed Anthropic web_search call",
                )
            evidence_text, sources, citations, truncated = extract_anthropic_web_search_evidence(
                capture.responses,
                provider=self.provider_profile,
                request=request,
            )
            if not evidence_text:
                evidence_text = completion.response_text or streamed_text
            return WebSearchResult(
                query=request.query,
                evidence_text=evidence_text,
                sources=sources,
                citations=citations,
                provider_profile=self.provider_profile,
                model=self.model,
                truncated=truncated,
                metadata={"auxiliary": True, "source_count": len(sources)},
            )
        except asyncio.CancelledError:
            raise
        except WebSearchError:
            raise
        except (ConfigurationError, ProviderError, OSError, UnicodeError) as error:
            raise self._normalize_error(error) from error


class GeminiHostedWebSearchBackend(ResponsesHostedWebSearchBackend):
    """One Gemini Interactions profile used as a forced Google Search sidecar."""

    def __init__(
        self,
        profile: ProviderProfile,
        provider_factory: Callable[
            [Callable[[Mapping[str, Any]], None], WebSearchRequest], ModelProvider
        ],
    ) -> None:
        self._profile = profile
        self._provider_factory = provider_factory
        self._capabilities = profile.effective_capabilities(
            GeminiInteractionsProvider.implementation_capabilities(
                model=profile.model,
                builtin_tools=profile.builtin_tools,
            )
        )

    async def search(
        self,
        request: WebSearchRequest,
        *,
        event_sink: HostedWebSearchEventSink | None = None,
    ) -> WebSearchResult:
        if (
            self._capabilities.status(ModelCapability.HOSTED_WEB_SEARCH)
            is not CapabilityStatus.SUPPORTED
        ):
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNSUPPORTED,
                "hosted web search is not explicitly supported by this profile",
            )
        capture = _ResponseCapture()
        try:
            provider = self._provider_factory(capture.observe, request)
            context = ModelContext(
                (
                    Message(Role.SYSTEM, SIDE_CAR_SEARCH_SYSTEM_PROMPT),
                    Message(Role.USER, _bounded_prompt(request)),
                )
            )
            completion: ModelCompleted | None = None
            streamed_text = ""
            async for event in provider.stream(
                context,
                (),
                tool_policy=ModelToolPolicy.ALLOWED,
            ):
                if isinstance(event, (ModelBackendToolStarted, ModelBackendToolCompleted)):
                    if event_sink is not None:
                        await event_sink(
                            HostedWebSearchEvent(
                                self.provider_profile,
                                self.model,
                                event.call_id,
                                event.name,
                                isinstance(event, ModelBackendToolCompleted),
                            )
                        )
                elif isinstance(event, ModelTextDelta):
                    streamed_text += event.text
                elif isinstance(event, ModelCompleted):
                    completion = event
            if completion is None:
                raise ProviderError("hosted search stream ended without completion")
            if capture.response is None:
                raise ProviderError("hosted search response did not expose structured evidence")
            if not _has_completed_gemini_google_search_execution(capture.response):
                raise WebSearchError(
                    WebSearchErrorCode.SEARCH_PROVIDER_DID_NOT_SEARCH,
                    "hosted search provider returned no completed google_search result",
                )
            evidence_text, sources, citations, truncated = extract_gemini_web_search_evidence(
                capture.response,
                provider=self.provider_profile,
                request=request,
            )
            if not evidence_text:
                evidence_text = completion.response_text or streamed_text
            return WebSearchResult(
                query=request.query,
                evidence_text=evidence_text,
                sources=sources,
                citations=citations,
                provider_profile=self.provider_profile,
                model=self.model,
                truncated=truncated,
                metadata={"auxiliary": True, "source_count": len(sources)},
            )
        except asyncio.CancelledError:
            raise
        except WebSearchError:
            raise
        except (ConfigurationError, ProviderError, OSError, UnicodeError) as error:
            raise self._normalize_error(error) from error


class RoutedHostedWebSearchBackend:
    """Explicit WEB_SEARCH-only failover over executable hosted candidates."""

    def __init__(self, candidates: Sequence[HostedWebSearch]) -> None:
        if not candidates:
            raise ValueError("hosted search failover requires candidates")
        self._candidates = tuple(candidates)
        self._active_index: int | None = None

    @property
    def provider_profile(self) -> str:
        index = self._active_index if self._active_index is not None else 0
        return self._candidates[index].provider_profile

    @property
    def model(self) -> str:
        index = self._active_index if self._active_index is not None else 0
        return self._candidates[index].model

    @property
    def capabilities(self) -> ModelCapabilitySet:
        return ModelCapabilitySet.intersection(
            candidate.capabilities for candidate in self._candidates
        )

    async def search(
        self,
        request: WebSearchRequest,
        *,
        event_sink: HostedWebSearchEventSink | None = None,
    ) -> WebSearchResult:
        start = self._active_index if self._active_index is not None else 0
        failures: list[str] = []
        for index in range(start, len(self._candidates)):
            candidate = self._candidates[index]
            try:
                result = await candidate.search(request, event_sink=event_sink)
            except asyncio.CancelledError:
                raise
            except WebSearchError as error:
                failures.append(f"{candidate.provider_profile}: {error.code.value}")
                continue
            self._active_index = index
            return result
        detail = "; ".join(failures)[:_MAX_FAILURE_DETAIL]
        failure_codes = tuple(
            failure.rsplit(": ", 1)[-1] for failure in failures if ": " in failure
        )
        code = (
            WebSearchErrorCode(failure_codes[0])
            if failure_codes and len(set(failure_codes)) == 1
            else WebSearchErrorCode.SEARCH_PROVIDER_ERROR
        )
        raise WebSearchError(
            code,
            f"all configured WEB_SEARCH providers failed: {detail}",
        )


class RoutedWebSearchBackendResolver(WebSearchBackendResolver):
    """Resolve only the configured WEB_SEARCH route and its own fallbacks."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def resolve(self, route: ModelRoute) -> HostedWebSearch | None:
        if route.role is not RuntimeRole.WEB_SEARCH:
            return None
        names = (route.provider_profile, *route.fallback_profiles)
        candidates: list[HostedWebSearch] = []
        for index, name in enumerate(dict.fromkeys(names)):
            profile = self._config.providers.get(name)
            if profile is None or profile.protocol not in {
                "openai-responses",
                "anthropic-messages",
                "gemini-interactions",
            }:
                continue
            selected = replace(profile, model=route.model) if index == 0 else profile
            try:
                if selected.protocol == "anthropic-messages":
                    implementation = AnthropicProvider.implementation_capabilities(
                        model=selected.model,
                        builtin_tools=selected.builtin_tools,
                    )
                elif selected.protocol == "gemini-interactions":
                    implementation = GeminiInteractionsProvider.implementation_capabilities(
                        model=selected.model,
                        builtin_tools=selected.builtin_tools,
                    )
                else:
                    implementation = OpenAIResponsesProvider.implementation_capabilities(
                        dialect=selected.dialect,
                        builtin_tools=selected.builtin_tools,
                    )
                capabilities = selected.effective_capabilities(implementation)
            except (ConfigurationError, ValueError, TypeError):
                continue
            if (
                capabilities.status(ModelCapability.HOSTED_WEB_SEARCH)
                is not CapabilityStatus.SUPPORTED
            ):
                continue
            sidecar_tools = ["web_search"]
            if (
                selected.protocol == "anthropic-messages"
                and capabilities.status(ModelCapability.HOSTED_WEB_FETCH)
                is CapabilityStatus.SUPPORTED
                and "web_fetch" in selected.builtin_tools
            ):
                sidecar_tools.append("web_fetch")
            if selected.protocol == "gemini-interactions":
                if "google_search" not in selected.builtin_tools:
                    continue
                sidecar_tools = ["google_search"]
                if (
                    "url_context" in selected.builtin_tools
                    and capabilities.status(ModelCapability.HOSTED_WEB_FETCH)
                    is CapabilityStatus.SUPPORTED
                    and capabilities.supports(ModelCapability.MIXED_HOSTED_AND_CLIENT_TOOLS)
                ):
                    sidecar_tools.append("url_context")
            sidecar_profile = replace(selected, builtin_tools=tuple(sidecar_tools))

            def provider_factory(
                observer: Callable[[Mapping[str, Any]], None],
                request: WebSearchRequest,
                *,
                selected_profile: ProviderProfile = sidecar_profile,
            ) -> ModelProvider:
                gemini_allowed_tools = ["google_search"]
                if "url_context" in selected_profile.builtin_tools:
                    # The current Interactions schema can restrict the set of
                    # allowed tools, but cannot express "must call A and may
                    # then call B" in one interaction. Allow both for the
                    # supported combination and keep the completed Search
                    # lifecycle as the backend success gate.
                    gemini_allowed_tools.append("url_context")
                return create_provider(
                    selected_profile,
                    response_observer=observer,
                    builtin_tool_options=_provider_tool_options(selected_profile, request),
                    tool_choice=(
                        {"type": "tool", "name": "web_search"}
                        if selected_profile.protocol == "anthropic-messages"
                        else {
                            "allowed_tools": {
                                "mode": "any",
                                "tools": gemini_allowed_tools,
                            }
                        }
                        if selected_profile.protocol == "gemini-interactions"
                        else "required"
                    ),
                )

            if selected.protocol == "anthropic-messages":
                candidates.append(
                    AnthropicHostedWebSearchBackend(sidecar_profile, provider_factory)
                )
            elif selected.protocol == "gemini-interactions":
                candidates.append(GeminiHostedWebSearchBackend(sidecar_profile, provider_factory))
            else:
                candidates.append(
                    ResponsesHostedWebSearchBackend(sidecar_profile, provider_factory)
                )
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        return RoutedHostedWebSearchBackend(candidates)


__all__ = [
    "SIDE_CAR_SEARCH_SYSTEM_PROMPT",
    "AnthropicHostedWebSearchBackend",
    "GeminiHostedWebSearchBackend",
    "ResponsesHostedWebSearchBackend",
    "RoutedHostedWebSearchBackend",
    "RoutedWebSearchBackendResolver",
    "extract_anthropic_web_search_evidence",
    "extract_gemini_web_search_evidence",
    "extract_web_search_evidence",
]
