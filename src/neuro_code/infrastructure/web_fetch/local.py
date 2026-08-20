"""Public-internet-only local Web Fetcher.

本地仅允许公网目标的 Web Fetcher.

This adapter owns DNS pinning, TLS, HTTP redirects, response bounds, MIME
policy, and text extraction.  It intentionally has no provider proxy policy,
cookies, authentication, browser engine, JavaScript runtime, or cache.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import re
import socket
import ssl
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, TCPConnector
from aiohttp.abc import AbstractResolver, ResolveResult

from neuro_code.application.ports.web_fetch import (
    MAX_FETCH_MAX_CHARS,
    WebFetchBackend,
    WebFetchError,
    WebFetchErrorCode,
    WebFetchProvenance,
    WebFetchRequest,
    WebFetchResult,
    normalize_web_fetch_url,
)

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_HEADER_COUNT = 128
MAX_HEADER_LINE_BYTES = 8_192
MAX_HEADER_VALUE_BYTES = 8_192
MAX_CHUNK_BYTES = 64 * 1024
DEFAULT_TOTAL_TIMEOUT_SECONDS = 30.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 10.0

LOCAL_WEB_FETCH_USER_AGENT = "Neuro-Code/0.1 local-web-fetch"
LOCAL_WEB_FETCH_ACCEPT = (
    "text/html,application/xhtml+xml,text/plain,text/markdown,application/json,"
    "application/xml,text/xml;q=0.9,*/*;q=0.1"
)
_REQUEST_HEADERS = {
    "User-Agent": LOCAL_WEB_FETCH_USER_AGENT,
    "Accept": LOCAL_WEB_FETCH_ACCEPT,
    "Accept-Encoding": "gzip",
}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "application/json",
        "application/xml",
        "text/xml",
    }
)
_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_LOCAL_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "instance-data",
        "metadata.google.internal",
    }
)
_NON_TEXT_BYTES = frozenset(
    {
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        11,
        12,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
    }
)
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_MARKUP_RE = re.compile(r"<[^>]*>")
_CHARSET_RE = re.compile(r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\"']+)", re.IGNORECASE)


def is_public_destination(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an address is globally routable for this fetcher.

    IPv4-mapped IPv6 values are normalized before the decision.  All
    non-global classes (loopback, private, link-local, multicast, reserved,
    unspecified, and shared address space) fail closed.
    """

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _unique_addresses(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


class PinnedPublicResolver(AbstractResolver):
    """Resolve once, validate every candidate, and return the pinned set.

    ``preflight`` performs the only hostname lookup for a request hop.  The
    same resolver instance is passed to ``TCPConnector``; its ``resolve``
    method never performs a second lookup, so a DNS answer change cannot move
    the socket to a newly unsafe address between validation and connection.
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host.casefold().rstrip(".")
        self._port = port
        self._addresses: tuple[str, ...] = ()

    async def preflight(self) -> None:
        try:
            literal = ipaddress.ip_address(self._host)
        except ValueError:
            literal = None
        addresses: tuple[str, ...]
        if literal is not None:
            if not is_public_destination(literal):
                raise WebFetchError(
                    WebFetchErrorCode.UNSAFE_DESTINATION,
                    "destination address is not public",
                )
            addresses = (str(literal),)
        else:
            if self._host in _LOCAL_HOSTNAMES:
                raise WebFetchError(
                    WebFetchErrorCode.UNSAFE_DESTINATION,
                    "destination hostname is not allowed",
                )
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    self._host,
                    self._port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                )
            except (OSError, socket.gaierror) as error:
                raise WebFetchError(
                    WebFetchErrorCode.DNS_FAILURE,
                    "destination DNS lookup failed",
                ) from error
            candidates: list[str] = []
            for _family, _socktype, _proto, _canonname, sockaddr in infos:
                candidate = sockaddr[0]
                if isinstance(candidate, str):
                    candidates.append(candidate)
            addresses = _unique_addresses(candidates)
            if not addresses:
                raise WebFetchError(
                    WebFetchErrorCode.DNS_FAILURE,
                    "destination DNS lookup returned no address",
                )
            if any(not is_public_destination(address) for address in addresses):
                raise WebFetchError(
                    WebFetchErrorCode.DNS_UNSAFE_RESULT,
                    "destination DNS lookup returned a non-public address",
                )
        self._addresses = addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        if host.casefold().rstrip(".") != self._host or port != self._port:
            raise OSError("resolver received an unpinned destination")
        if not self._addresses:
            raise OSError("resolver was used before destination preflight")
        results: list[ResolveResult] = []
        for address in self._addresses:
            parsed = ipaddress.ip_address(address)
            address_family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
            if family not in {socket.AF_UNSPEC, address_family}:
                continue
            results.append(
                ResolveResult(
                    hostname=self._host,
                    host=address,
                    port=self._port,
                    family=address_family,
                    proto=socket.IPPROTO_TCP,
                    flags=socket.AI_NUMERICHOST,
                )
            )
        return results

    async def close(self) -> None:
        return None


class _HtmlMarkdownExtractor(HTMLParser):
    """Small, non-executing HTML-to-Markdown projection."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "canvas"})
    _BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "dd",
            "div",
            "dl",
            "dt",
            "footer",
            "form",
            "header",
            "hr",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "td",
            "th",
            "tr",
            "ul",
        }
    )

    def __init__(self, *, max_chars: int) -> None:
        super().__init__(convert_charrefs=True)
        self._max_chars = max(1_000, min(max_chars * 4, MAX_FETCH_MAX_CHARS))
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._skip_depth = 0
        self._title_depth = 0
        self._pre_depth = 0
        self._heading_level: int | None = None
        self._truncated = False

    def _append(self, value: str) -> None:
        if not value or self._truncated:
            return
        current = sum(len(part) for part in self._parts)
        remaining = self._max_chars - current
        if remaining <= 0:
            self._truncated = True
            return
        if len(value) > remaining:
            self._parts.append(value[:remaining])
            self._truncated = True
        else:
            self._parts.append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._title_depth += 1
            return
        if tag == "pre":
            self._pre_depth += 1
            self._append("\n\n```\n")
            return
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            level = min(6, int(tag[1]))
            self._heading_level = level
            self._append("\n\n" + ("#" * level) + " ")
            return
        if tag == "li":
            self._append("\n- ")
            return
        if tag == "br":
            self._append("\n")
            return
        if tag in self._BLOCK_TAGS:
            self._append("\n\n")
        elif tag in {"strong", "b"}:
            self._append("**")
        elif tag in {"em", "i"}:
            self._append("*")
        elif tag == "code" and not self._pre_depth:
            self._append("`")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._title_depth = max(0, self._title_depth - 1)
            return
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            self._append("\n```\n")
            return
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            self._heading_level = None
            self._append("\n\n")
            return
        if tag in self._BLOCK_TAGS:
            self._append("\n\n")
        elif tag in {"strong", "b"}:
            self._append("**")
        elif tag in {"em", "i"}:
            self._append("*")
        elif tag == "code" and not self._pre_depth:
            self._append("`")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._title_depth:
            self._title_parts.append(data)
            return
        if self._pre_depth:
            self._append(data)
            return
        cleaned = _WHITESPACE_RE.sub(" ", data)
        if cleaned.strip():
            self._append(cleaned)

    def result(self) -> tuple[str, str, bool]:
        content = _MULTI_NEWLINE_RE.sub("\n\n", "".join(self._parts))
        lines = [line.rstrip() for line in content.splitlines()]
        content = "\n".join(lines).strip()
        title = _MARKUP_RE.sub("", "".join(self._title_parts))
        title = _WHITESPACE_RE.sub(" ", title).strip()
        return content, title[:512], self._truncated


def _parse_media_type(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if len(value.encode("utf-8", "ignore")) > MAX_HEADER_VALUE_BYTES:
        raise WebFetchError(
            WebFetchErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "content type header is too large",
        )
    media_type = value.split(";", 1)[0].strip().casefold()
    if not media_type:
        return None, None
    charset_match = _CHARSET_RE.search(value)
    charset = charset_match.group(1).strip()[:64] if charset_match else None
    return media_type, charset


def _sniff_media_type(body: bytes) -> str:
    sample = body[:512].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not sample:
        raise WebFetchError(
            WebFetchErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "response has no content type or content",
        )
    if b"\x00" in sample or any(byte in _NON_TEXT_BYTES for byte in sample):
        raise WebFetchError(
            WebFetchErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "response content is not text",
        )
    try:
        decoded = sample.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WebFetchError(
            WebFetchErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "response content is not recognized text",
        ) from error
    lowered = decoded.casefold()
    if decoded.startswith(("{", "[")):
        return "application/json"
    if decoded.startswith("<"):
        if lowered.startswith(("<?xml", "<!doctype svg")):
            return "application/xml"
        return "text/html"
    return "text/plain"


def _decode_body(body: bytes, *, charset: str | None) -> str:
    selected = "utf-8"
    if charset:
        try:
            selected = codecs.lookup(charset).name
        except LookupError:
            selected = "utf-8"
    return body.decode(selected, errors="replace")


def _clean_text(content: str) -> str:
    lines = [line.rstrip() for line in content.replace("\r\n", "\n").splitlines()]
    return _MULTI_NEWLINE_RE.sub("\n\n", "\n".join(lines)).strip()


class LocalWebFetcher(WebFetchBackend):
    """Reusable policy object for one direct public-internet fetch."""

    def __init__(
        self,
        *,
        redaction_values: tuple[str, ...] = (),
        body_limit_bytes: int = MAX_BODY_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
    ) -> None:
        if not 1 <= body_limit_bytes <= MAX_BODY_BYTES:
            raise ValueError("body limit must be between 1 and the local maximum")
        if not 0 <= max_redirects <= MAX_REDIRECTS:
            raise ValueError("redirect limit is invalid")
        if min(total_timeout_seconds, connect_timeout_seconds, read_timeout_seconds) <= 0:
            raise ValueError("fetch timeouts must be positive")
        self._redaction_values = tuple(redaction_values)
        self._body_limit_bytes = body_limit_bytes
        self._max_redirects = max_redirects
        self._total_timeout_seconds = total_timeout_seconds
        self._timeout = ClientTimeout(
            total=total_timeout_seconds,
            connect=connect_timeout_seconds,
            sock_connect=connect_timeout_seconds,
            sock_read=read_timeout_seconds,
        )
        self._ssl_context = ssl.create_default_context()

    def _contains_secret(self, value: str) -> bool:
        decoded = unquote(value)
        return any(
            secret and (secret in value or secret in decoded) for secret in self._redaction_values
        )

    async def _pin_destination(self, url: str) -> PinnedPublicResolver:
        parsed = urlsplit(url)
        host = parsed.hostname
        if host is None:
            raise WebFetchError(WebFetchErrorCode.INVALID_URL, "destination hostname is missing")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as error:
            raise WebFetchError(
                WebFetchErrorCode.INVALID_URL, "destination port is invalid"
            ) from error
        try:
            normalized_host = host.encode("idna").decode("ascii").rstrip(".").casefold()
        except UnicodeError as error:
            raise WebFetchError(
                WebFetchErrorCode.INVALID_URL, "destination hostname is invalid"
            ) from error
        resolver = PinnedPublicResolver(normalized_host, port)
        try:
            await asyncio.wait_for(
                resolver.preflight(),
                timeout=self._total_timeout_seconds,
            )
        except TimeoutError as error:
            raise WebFetchError(WebFetchErrorCode.TIMEOUT, "web fetch timed out") from error
        return resolver

    async def _read_body(self, response: aiohttp.ClientResponse) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError as error:
                raise WebFetchError(
                    WebFetchErrorCode.HTTP_ERROR,
                    "response content length is invalid",
                ) from error
            if declared < 0:
                raise WebFetchError(
                    WebFetchErrorCode.HTTP_ERROR,
                    "response content length is invalid",
                )
            if declared > self._body_limit_bytes:
                raise WebFetchError(
                    WebFetchErrorCode.BODY_TOO_LARGE,
                    "response body exceeds the configured limit",
                )
        content = bytearray()
        async for chunk in response.content.iter_chunked(MAX_CHUNK_BYTES):
            content.extend(chunk)
            if len(content) > self._body_limit_bytes:
                raise WebFetchError(
                    WebFetchErrorCode.BODY_TOO_LARGE,
                    "response body exceeds the configured limit",
                )
        return bytes(content)

    async def _request_once(
        self,
        url: str,
    ) -> tuple[int, str | None, bytes, str | None, str | None]:
        resolver = await self._pin_destination(url)
        connector = TCPConnector(
            ssl=self._ssl_context,
            resolver=resolver,
            use_dns_cache=False,
            limit=1,
            limit_per_host=1,
            force_close=True,
            enable_cleanup_closed=True,
        )
        session = ClientSession(
            connector=connector,
            connector_owner=True,
            cookie_jar=DummyCookieJar(),
            timeout=self._timeout,
            auto_decompress=True,
            trust_env=False,
            max_headers=MAX_HEADER_COUNT,
            max_line_size=MAX_HEADER_LINE_BYTES,
            max_field_size=MAX_HEADER_VALUE_BYTES,
        )
        try:
            async with session.get(
                url,
                headers=_REQUEST_HEADERS,
                allow_redirects=False,
                proxy=None,
            ) as response:
                if response.status in _REDIRECT_STATUSES:
                    return response.status, response.headers.get("Location"), b"", None, None
                if not 200 <= response.status < 300:
                    raise WebFetchError(
                        WebFetchErrorCode.HTTP_ERROR,
                        "HTTP response was not successful",
                        status_code=response.status,
                    )
                declared_media_type, charset = _parse_media_type(
                    response.headers.get("Content-Type")
                )
                if (
                    declared_media_type is not None
                    and declared_media_type not in _ALLOWED_MEDIA_TYPES
                ):
                    raise WebFetchError(
                        WebFetchErrorCode.UNSUPPORTED_MEDIA_TYPE,
                        "response media type is not allowed",
                    )
                body = await self._read_body(response)
                return response.status, None, body, declared_media_type, charset
        except WebFetchError:
            raise
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise WebFetchError(WebFetchErrorCode.TIMEOUT, "web fetch timed out") from error
        except (
            aiohttp.ClientConnectorCertificateError,
            aiohttp.ClientConnectorSSLError,
            aiohttp.ClientSSLError,
        ) as error:
            raise WebFetchError(WebFetchErrorCode.TLS_ERROR, "TLS connection failed") from error
        except aiohttp.ClientConnectorDNSError as error:
            raise WebFetchError(
                WebFetchErrorCode.DNS_FAILURE, "destination DNS lookup failed"
            ) from error
        except aiohttp.InvalidURL as error:
            raise WebFetchError(
                WebFetchErrorCode.INVALID_URL, "HTTP client rejected the URL"
            ) from error
        except (aiohttp.ClientError, OSError) as error:
            raise WebFetchError(WebFetchErrorCode.HTTP_ERROR, "HTTP request failed") from error
        finally:
            await session.close()

    def _redirect_url(self, current_url: str, location: str | None) -> str:
        if not location:
            raise WebFetchError(
                WebFetchErrorCode.HTTP_ERROR,
                "redirect response did not contain a location",
            )
        try:
            candidate = urljoin(current_url, location)
            next_url = normalize_web_fetch_url(candidate)
        except WebFetchError as error:
            if error.code is WebFetchErrorCode.SECRET_IN_URL:
                raise
            raise WebFetchError(
                WebFetchErrorCode.REDIRECT_UNSAFE,
                "redirect target is not allowed",
            ) from error
        except (TypeError, ValueError) as error:
            raise WebFetchError(
                WebFetchErrorCode.REDIRECT_UNSAFE,
                "redirect target is not allowed",
            ) from error
        if urlsplit(current_url).scheme == "https" and urlsplit(next_url).scheme != "https":
            raise WebFetchError(
                WebFetchErrorCode.REDIRECT_UNSAFE,
                "HTTPS to HTTP redirects are not allowed",
            )
        if self._contains_secret(next_url):
            raise WebFetchError(
                WebFetchErrorCode.SECRET_IN_URL,
                "redirect URL contains a configured secret",
            )
        return next_url

    def _extract(
        self, body: bytes, *, media_type: str, charset: str | None, max_chars: int
    ) -> tuple[str, str, bool]:
        decoded = _decode_body(body, charset=charset)
        if media_type in _HTML_MEDIA_TYPES:
            try:
                parser = _HtmlMarkdownExtractor(max_chars=max_chars)
                parser.feed(decoded)
                parser.close()
                content, title, parser_truncated = parser.result()
                return content, title, parser_truncated
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise WebFetchError(
                    WebFetchErrorCode.EXTRACTION_FAILED,
                    "HTML extraction failed",
                ) from error
        return _clean_text(decoded), "", False

    async def fetch(self, request: WebFetchRequest) -> WebFetchResult:
        try:
            return await asyncio.wait_for(
                self._fetch_without_total_timeout(request),
                timeout=self._total_timeout_seconds,
            )
        except WebFetchError:
            raise
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise WebFetchError(WebFetchErrorCode.TIMEOUT, "web fetch timed out") from error

    async def _fetch_without_total_timeout(self, request: WebFetchRequest) -> WebFetchResult:
        if request.max_chars > MAX_FETCH_MAX_CHARS:
            raise WebFetchError(WebFetchErrorCode.INVALID_URL, "fetch request is out of bounds")
        current_url = request.url
        if self._contains_secret(current_url):
            raise WebFetchError(
                WebFetchErrorCode.SECRET_IN_URL,
                "fetch URL contains a configured secret",
            )
        redirects = 0
        redirected = False
        while True:
            try:
                status, location, body, declared_media_type, charset = await self._request_once(
                    current_url
                )
            except WebFetchError as error:
                if redirected and error.code in {
                    WebFetchErrorCode.UNSAFE_DESTINATION,
                    WebFetchErrorCode.DNS_UNSAFE_RESULT,
                }:
                    raise WebFetchError(
                        WebFetchErrorCode.REDIRECT_UNSAFE,
                        "redirect destination is not allowed",
                    ) from error
                raise
            if status in _REDIRECT_STATUSES:
                if redirects >= self._max_redirects:
                    raise WebFetchError(
                        WebFetchErrorCode.TOO_MANY_REDIRECTS,
                        "redirect limit exceeded",
                    )
                current_url = self._redirect_url(current_url, location)
                redirects += 1
                redirected = True
                continue

            # The response headers are intentionally reduced to the two
            # decoding inputs needed by the extractor.  No headers cross the
            # application/tool boundary.
            media_type = declared_media_type or _sniff_media_type(body)
            if media_type not in _ALLOWED_MEDIA_TYPES:
                raise WebFetchError(
                    WebFetchErrorCode.UNSUPPORTED_MEDIA_TYPE,
                    "response media type is not allowed",
                )
            content, title, extracted_truncated = self._extract(
                body,
                media_type=media_type,
                charset=charset,
                max_chars=request.max_chars,
            )
            if len(content) > request.max_chars:
                content = content[: request.max_chars]
                extracted_truncated = True
            return WebFetchResult(
                requested_url=request.url,
                final_url=current_url,
                title=title,
                media_type=media_type,
                content=content,
                status_code=status,
                truncated=extracted_truncated,
                provenance=(
                    WebFetchProvenance.FETCH_REDIRECT if redirected else request.provenance
                ),
                metadata={"redirect_count": redirects},
            )


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "DEFAULT_TOTAL_TIMEOUT_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_HEADER_COUNT",
    "MAX_HEADER_LINE_BYTES",
    "MAX_HEADER_VALUE_BYTES",
    "MAX_REDIRECTS",
    "LocalWebFetcher",
    "PinnedPublicResolver",
    "is_public_destination",
]
