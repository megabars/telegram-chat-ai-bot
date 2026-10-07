"""Bounded, SSRF-resistant fetcher for user-supplied public web pages."""

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

MAX_PAGE_BYTES = 1_000_000
MAX_PAGE_CHARS = 20_000
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 12
SKIP_TAGS = {"script", "style", "noscript", "svg", "header", "footer", "nav"}


class UrlFetchError(ValueError):
    """The supplied URL could not be fetched safely as a readable page."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipped = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in SKIP_TAGS:
            self.skipped += 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self.skipped:
            self.skipped -= 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipped:
            self.parts.append(data)


def _normalize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise UrlFetchError("Ссылка выглядит некорректно.") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 80, 443)
        or any(ord(char) < 32 or char.isspace() for char in value)
    ):
        raise UrlFetchError("Поддерживаются только публичные HTTP/HTTPS-ссылки без авторизации.")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


async def _public_addresses(host: str, port: int) -> list[dict]:
    if host.casefold() == "localhost" or host.casefold().endswith(".localhost"):
        raise UrlFetchError("Ссылки на локальные и внутренние адреса запрещены.")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal.compressed]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except OSError:
            raise UrlFetchError("Не удалось найти сервер по этой ссылке.") from None
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UrlFetchError("Ссылки на локальные и внутренние адреса запрещены.")
    family_for = {socket.AF_INET: socket.AF_INET, socket.AF_INET6: socket.AF_INET6}
    return [
        {
            "hostname": host,
            "host": address,
            "port": port,
            "family": family_for[socket.AF_INET6 if ":" in address else socket.AF_INET],
            "proto": 0,
            "flags": 0,
        }
        for address in addresses
    ]


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """Use the already-validated DNS answer, preventing a second lookup/rebinding."""

    def __init__(self, records: list[dict]) -> None:
        self.records = records

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return self.records

    async def close(self) -> None:
        return None


async def _read_body(response: aiohttp.ClientResponse) -> bytes:
    if response.content_length is not None and response.content_length > MAX_PAGE_BYTES:
        raise UrlFetchError("Страница слишком большая для чтения.")
    data = bytearray()
    async for chunk in response.content.iter_chunked(16_384):
        data.extend(chunk)
        if len(data) > MAX_PAGE_BYTES:
            raise UrlFetchError("Страница слишком большая для чтения.")
    return bytes(data)


def _extract_text(raw: bytes, content_type: str, charset: str | None) -> str:
    if "text/html" in content_type or "application/xhtml+xml" in content_type:
        source = raw.decode(charset or "utf-8", errors="replace")
        parser = _TextExtractor()
        parser.feed(source)
        text = " ".join(" ".join(parser.parts).split())
    elif content_type.startswith("text/plain"):
        text = raw.decode(charset or "utf-8", errors="replace")
        text = " ".join(text.split())
    else:
        raise UrlFetchError("По этой ссылке нужна HTML-страница или обычный текстовый файл.")
    if not text:
        raise UrlFetchError("На странице не найден читаемый текст.")
    return text[:MAX_PAGE_CHARS]


async def fetch_page(url: str) -> tuple[str, str]:
    """Fetch a public HTML/text URL with strict network, size and redirect bounds."""
    current = _normalize_url(url)
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS, connect=5, sock_read=8)
    for redirect_count in range(MAX_REDIRECTS + 1):
        parsed = urlsplit(current)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        records = await _public_addresses(host, port)
        connector = aiohttp.TCPConnector(resolver=_PinnedResolver(records), use_dns_cache=False)
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                trust_env=False,
                headers={"User-Agent": "TelegramChatAIBot/1.6 (link reader)"},
            ) as session:
                async with session.get(current, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or redirect_count == MAX_REDIRECTS:
                            raise UrlFetchError("Слишком много перенаправлений по ссылке.")
                        current = _normalize_url(urljoin(current, location))
                        continue
                    if response.status >= 400:
                        raise UrlFetchError(f"Сайт вернул HTTP {response.status}.")
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    charset = response.charset
                    raw = await _read_body(response)
                    return current, _extract_text(raw, content_type, charset)
        except asyncio.CancelledError:
            raise
        except UrlFetchError:
            raise
        except (aiohttp.ClientError, TimeoutError, UnicodeError):
            raise UrlFetchError("Не удалось загрузить страницу по этой ссылке.") from None
    raise UrlFetchError("Не удалось загрузить страницу по этой ссылке.")
