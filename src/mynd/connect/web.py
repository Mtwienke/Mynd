"""Selectable web research backends; all synthesis stays on the local model."""
from __future__ import annotations

import ipaddress
import os
import re
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from mynd.vault.safety import digest


def public_url(value: str) -> bool:
    if re.search(r"[\s<>\\\x00-\x1f\x7f]", value):
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443}):
            return False
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            numeric = re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host)
            return "." in host and numeric is None
    except ValueError:
        return False


class WebAccessError(ValueError):
    """An account-level refusal that should not be hidden as a failed page."""


class OllamaWeb:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key if api_key is not None else os.environ.get("OLLAMA_API_KEY", "")
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, connect=10))
        self._access_error: str | None = None

    def _post(self, endpoint: str, payload: dict):
        if self._access_error:
            raise WebAccessError(self._access_error)
        if not self.api_key:
            raise ValueError(
                "Set OLLAMA_API_KEY in .keys or the environment, "
                "or use topic_web_provider = 'ddgs' for key-free search"
            )
        response = self.client.post(
            "https://ollama.com/api/" + endpoint, json=payload,
            headers={"Authorization": "Bearer " + self.api_key},
        )
        if response.status_code == 402:
            # Do not echo arbitrary response bodies, headers, or credentials.
            self._access_error = (
                "Ollama web search/fetch requires usage credits for this account (HTTP 402). "
                "Its advertised free tier is not available for this request. "
                "Use topic_web_provider = 'ddgs' for key-free search, "
                "or check https://ollama.com/settings/billing"
            )
            raise WebAccessError(self._access_error)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("Ollama search/fetch returned an invalid response")
        return value

    def research(self, query: str) -> dict:
        value = self._post("web_search", {"query": query, "max_results": 5})
        results = value.get("results")
        if not isinstance(results, list):
            raise TypeError("Ollama search returned no result list")
        sources, seen, errors = [], set(), []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = item.get("url", "")
            if not isinstance(url, str) or not public_url(url) or url in seen:
                continue
            seen.add(url)
            try:
                page = self._post("web_fetch", {"url": url})
                text = page.get("content")
                title = page.get("title") or item.get("title") or url
                if not isinstance(text, str) or len(text.strip()) < 80:
                    raise ValueError("Fetched page has insufficient usable content")
                if not isinstance(title, str):
                    title = url
                text = text[:20_000]
                sources.append({"id": "web-" + digest(url + "\0" + text)[:24],
                                "url": url, "title": title[:300], "text": text})
                if len(sources) == 3:
                    break
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                if isinstance(exc, WebAccessError):
                    raise
                # Do not include request headers, keys, or full response bodies in logs.
                errors.append(type(exc).__name__)
        if not sources:
            raise ValueError("No usable web pages were fetched" + (": " + ", ".join(errors) if errors else ""))
        return {"query": query, "provider": "ollama", "fetched_at": datetime.now(UTC).isoformat(),
                "sources": sources}

    def close(self):
        self.client.close()


class WebResearch(Protocol):
    def research(self, query: str) -> dict: ...

    def close(self) -> None: ...


def build_web_client(provider: str) -> WebResearch:
    if provider == "ollama":
        return OllamaWeb()
    if provider == "ddgs":
        # Offline Connect does not import or initialize the search library.
        from mynd.connect.web_ddgs import DDGSWeb

        return DDGSWeb()
    raise ValueError("Unknown web provider; choose 'ddgs' or 'ollama'")
