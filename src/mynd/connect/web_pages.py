"""Bounded, robots-aware public-page fetching without scripts or hosted readers."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from lxml import etree, html

from mynd.connect.web import public_url

USER_AGENT = "Mynd/0.1"
MAX_PAGE_BYTES = 2_000_000
MAX_ROBOTS_BYTES = 256_000
MAX_REDIRECTS = 5
_PAGE_TYPES = {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}


class PageFetchError(ValueError):
    """Safe, fixed diagnostic text, never a remote response body."""


@dataclass(frozen=True)
class _Page:
    url: str
    status: int
    content_type: str
    text: str


def _public_target(url: str) -> tuple[httpx.URL, str]:
    """Resolve and pin a public address, including on every redirect."""
    if not public_url(url):
        raise PageFetchError("Non-public or unsafe page URL")
    target = httpx.URL(url)
    if "%" in target.host:
        raise PageFetchError("Scoped addresses are not public page hosts")
    port = target.port or (443 if target.scheme == "https" else 80)
    addresses = {
        ipaddress.ip_address(item[4][0])
        for item in socket.getaddrinfo(target.host, port, type=socket.SOCK_STREAM)
    }
    if not addresses or any(not address.is_global for address in addresses):
        raise PageFetchError("Page host resolves to a non-public address")
    # Prefer IPv4 on hosts without a working IPv6 route. Do not resolve the
    # hostname again at connection time (DNS rebinding).
    address = min(addresses, key=lambda item: (item.version, int(item)))
    return target, str(address)


class PageFetcher:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(20, connect=10),
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        self._robots: dict[str, RobotFileParser] = {}

    def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        if origin not in self._robots:
            page = self._get(origin + "/robots.txt", robots=True)
            parser = RobotFileParser()
            if page.status in {401, 403}:
                parser.disallow_all = True
            elif page.status in {404, 410}:
                parser.allow_all = True
            else:
                parser.parse(page.text.splitlines())
            self._robots[origin] = parser
        if not self._robots[origin].can_fetch(USER_AGENT, url):
            raise PageFetchError("robots.txt disallows this page")

    def _get(self, url: str, *, robots: bool = False) -> _Page:
        limit = MAX_ROBOTS_BYTES if robots else MAX_PAGE_BYTES
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            target, address = _public_target(current)
            if not robots:
                self._check_robots(current)
            # Pinned IP URLs must not make unrelated virtual hosts share cookies.
            # This is a stateless reader, not the user's browser session.
            self.client.cookies.clear()
            # Connect only to the validated IP, keeping Host and TLS certificate
            # verification tied to the original hostname. Never forward API keys.
            with self.client.stream(
                "GET", target.copy_with(host=address),
                headers={
                    "Host": target.netloc.decode("ascii"),
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html, application/xhtml+xml, text/plain, text/markdown",
                    "Connection": "close",
                },
                extensions={"sni_hostname": target.host},
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise PageFetchError("Page redirect has no destination")
                    current = urljoin(current, location)
                    continue
                if not (robots and response.status_code in {401, 403, 404, 410}):
                    response.raise_for_status()
                kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if not robots and kind not in _PAGE_TYPES:
                    raise PageFetchError("Page is not HTML or plain text")
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > limit:
                    raise PageFetchError("Page exceeds the download size limit")
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=64_000):
                    body.extend(chunk)
                    if len(body) > limit:
                        raise PageFetchError("Page exceeds the download size limit")
                try:
                    text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
                except LookupError:
                    text = bytes(body).decode("utf-8", errors="replace")
                return _Page(current, response.status_code, kind, text)
        raise PageFetchError("Page exceeded the redirect limit")

    def fetch(self, url: str) -> dict:
        page = self._get(url)
        title = ""
        text = page.text
        if page.content_type in {"text/html", "application/xhtml+xml"}:
            try:
                document = html.fromstring(text, parser=html.HTMLParser(no_network=True))
                title = " ".join(document.xpath("string(//title)").split())
                if document.xpath(
                    "//*[@id='disambigbox' or "
                    "contains(concat(' ', normalize-space(@class), ' '), ' dmbox-disambig ')]"
                ) or "(disambiguation)" in title.lower():
                    raise PageFetchError("Page is a disambiguation index, not a topic overview")
                if title.lower() in {
                    "just a moment...", "access denied", "attention required! | cloudflare",
                }:
                    raise PageFetchError("Page requires an interactive browser")
                for element in document.xpath(
                    "//head|//script|//style|//noscript|//nav|//footer|//header|//aside|"
                    "//form|//svg|//iframe|//template|//*[@hidden]|//*[@aria-hidden='true']"
                ):
                    if element.getparent() is not None:
                        element.drop_tree()
                roots = document.xpath("//main") or document.xpath("//article") or [document]
                root = max(roots, key=lambda item: len(item.text_content()))
                text = " ".join(root.itertext())
            except (etree.LxmlError, ValueError) as exc:
                if isinstance(exc, PageFetchError):
                    raise
                raise PageFetchError("Page HTML could not be parsed") from None
        text = " ".join(text.split())[:20_000]
        if len(text) < 80:
            raise PageFetchError("Page has insufficient usable content")
        return {"url": page.url, "title": title[:300], "content": text}

    def close(self) -> None:
        self.client.close()
