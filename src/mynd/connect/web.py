"""Ollama's hosted search/fetch only; local inference stays on the existing backend."""
from __future__ import annotations

import ipaddress
import os
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from mynd.vault.safety import digest


def public_url(value: str) -> bool:
    if re.search(r"[\s<>\x00-\x1f]", value):
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


class OllamaWeb:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key if api_key is not None else os.environ.get("OLLAMA_API_KEY", "")
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, connect=10))

    def _post(self, endpoint: str, payload: dict):
        if not self.api_key:
            raise ValueError("Web overview pending: set OLLAMA_API_KEY (free Ollama account)")
        response = self.client.post(
            "https://ollama.com/api/" + endpoint, json=payload,
            headers={"Authorization": "Bearer " + self.api_key},
        )
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
                # Do not include request headers, keys, or full response bodies in logs.
                errors.append(type(exc).__name__)
        if not sources:
            raise ValueError("No usable web pages were fetched" + (": " + ", ".join(errors) if errors else ""))
        return {"query": query, "fetched_at": datetime.now(UTC).isoformat(),
                "sources": sources}

    def close(self):
        self.client.close()
