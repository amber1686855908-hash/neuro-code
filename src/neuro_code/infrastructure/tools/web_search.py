"""Model-facing sidecar web-search client tool.

面向模型的 Sidecar Web Search 客户端工具.

The local tool owns only the client-side ToolResult boundary.  The injected
application service owns route selection and the provider-hosted lifecycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.web_search import (
    MAX_DOMAIN_CHARS,
    MAX_DOMAIN_COUNT,
    MAX_MAX_SOURCES,
    MAX_QUERY_CHARS,
    MAX_TOTAL_RESULT_BYTES,
    WebSearchError,
    WebSearchErrorCode,
    WebSearchQueryPort,
    WebSearchRequest,
    WebSearchResult,
)
from neuro_code.domain.tools import ToolDefinition, ToolResult

WEB_SEARCH_TOOL_DESCRIPTION = (
    "Use web_search when the task requires current or external information that "
    "cannot be established reliably from the workspace. For technical questions, "
    "prefer official documentation, primary repositories, release notes, "
    "specifications, and maintainer sources. Do not search when repository evidence "
    "is sufficient."
)


def render_web_search_result(result: WebSearchResult) -> str:
    """Render canonical evidence with an explicit untrusted-content boundary."""

    lines = [
        "[UNTRUSTED WEB EVIDENCE]",
        f'Web search evidence for: "{result.query}"',
        "",
    ]
    citations_by_url: dict[str, list[str]] = {}
    for citation in result.citations:
        if citation.cited_text:
            citations_by_url.setdefault(citation.url.casefold(), []).append(citation.cited_text)
    for index, source in enumerate(result.sources, start=1):
        lines.extend(
            (
                f"Source {index}",
                f"Title: {source.title}",
                f"URL: {source.url}",
                f"Provider: {source.provider}",
            )
        )
        evidence = source.snippet or ""
        if not evidence:
            evidence = " ".join(citations_by_url.get(source.url.casefold(), ()))
        if evidence:
            lines.append(f"Evidence: {evidence}")
        lines.append("")
    lines.append("Synthesis:")
    lines.append(result.evidence_text or "No concise evidence was returned.")
    if result.truncated:
        lines.append("\n[Evidence truncated to the configured safety bounds]")
    rendered = "\n".join(lines)
    if len(rendered.encode()) <= MAX_TOTAL_RESULT_BYTES:
        return rendered
    encoded = rendered.encode()[:MAX_TOTAL_RESULT_BYTES]
    return encoded.decode("utf-8", "ignore") + "\n[Evidence truncated]"


class WebSearchTool:
    """A non-filesystem, externally-connected, model-visible search tool."""

    definition = ToolDefinition(
        name="web_search",
        description=WEB_SEARCH_TOOL_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_QUERY_CHARS,
                },
                "max_sources": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_MAX_SOURCES,
                    "default": 8,
                },
                "allowed_domains": {
                    "type": "array",
                    "maxItems": MAX_DOMAIN_COUNT,
                    "items": {"type": "string", "maxLength": MAX_DOMAIN_CHARS},
                },
                "blocked_domains": {
                    "type": "array",
                    "maxItems": MAX_DOMAIN_COUNT,
                    "items": {"type": "string", "maxLength": MAX_DOMAIN_CHARS},
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    def __init__(self, service: WebSearchQueryPort) -> None:
        self._service = service

    @property
    def side_effecting(self) -> bool:
        # This tool has no workspace/write/shell side effect.  Its external data
        # boundary is enforced by the service and the provider HTTP policy.
        return False

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult("web_search requires a non-empty string query", is_error=True)
        try:
            request = WebSearchRequest(
                query=query,
                max_sources=arguments.get("max_sources", 8),
                allowed_domains=arguments.get("allowed_domains", ()),
                blocked_domains=arguments.get("blocked_domains", ()),
            )
            result = await self._service.search(
                request,
                event_sink=context.web_search_event_sink,
            )
        except WebSearchError as error:
            return ToolResult(
                f"Web search failed ({error.code.value}): {error}",
                is_error=True,
                metadata={"error_code": error.code.value},
            )
        except (TypeError, ValueError):
            return ToolResult(
                f"Web search failed ({WebSearchErrorCode.SEARCH_INVALID_REQUEST.value}): "
                "the request did not satisfy the bounded search contract",
                is_error=True,
                metadata={"error_code": WebSearchErrorCode.SEARCH_INVALID_REQUEST.value},
            )
        return ToolResult(
            render_web_search_result(result),
            metadata={
                "provider_profile": result.provider_profile,
                "model": result.model,
                "source_count": len(result.sources),
                "citation_count": len(result.citations),
                "truncated": result.truncated,
                "external_data": True,
            },
        )


__all__ = ["WEB_SEARCH_TOOL_DESCRIPTION", "WebSearchTool", "render_web_search_result"]
