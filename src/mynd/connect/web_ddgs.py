"""Key-free search via DDGS and bounded local page extraction."""
from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from mynd.connect.web import public_url
from mynd.connect.web_pages import PageFetcher, PageFetchError
from mynd.vault.safety import digest

# Explicit engines avoid sending topic queries to every backend DDGS supports.
SEARCH_BACKENDS = "duckduckgo,brave,mojeek"


class DDGSWeb:
    def __init__(self, fetcher: PageFetcher | None = None):
        self.fetcher = fetcher or PageFetcher()

    def _search(self, query: str) -> list[dict]:
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException

        try:
            with DDGS(timeout=10) as search:
                try:
                    return search.text(query, max_results=5, backend=SEARCH_BACKENDS)
                except DDGSException:
                    # Topic overviews can still use a real public reference page
                    # when general engines challenge/rate-limit this connection.
                    # OpenSearch needs a topic term, not Mynd's default suffix.
                    term = re.sub(r"\s+definition\s+overview$", "", query, flags=re.IGNORECASE)
                    return search.text(term, max_results=1, backend="wikipedia")
        except DDGSException as exc:
            # DDGS errors may include raw responses; expose only the error type.
            raise ValueError(
                f"DDGS search unavailable ({type(exc).__name__}); "
                "search engines may be throttling or blocking requests. Try again later"
            ) from None

    def research(self, query: str) -> dict:
        results = self._search(query)
        if not isinstance(results, list):
            raise TypeError("DDGS returned an invalid result list")
        sources, seen, errors = [], set(), []
        for item in results[:5]:
            if not isinstance(item, dict):
                continue
            url = item.get("href", "")
            if not isinstance(url, str) or not public_url(url) or url in seen:
                continue
            seen.add(url)
            try:
                page = self.fetcher.fetch(url)
                final_url, text = page["url"], page["content"]
                if any(source["url"] == final_url for source in sources):
                    continue
                title = page["title"] or item.get("title") or final_url
                title = title if isinstance(title, str) else final_url
                sources.append({
                    "id": "web-" + digest(final_url + "\0" + text)[:24],
                    "url": final_url, "title": title[:300], "text": text,
                })
                if len(sources) == 3:
                    break
            except (httpx.HTTPError, OSError, ValueError) as exc:
                if isinstance(exc, PageFetchError):
                    errors.append(str(exc))
                elif isinstance(exc, httpx.HTTPStatusError):
                    errors.append("HTTP " + str(exc.response.status_code))
                else:
                    errors.append(type(exc).__name__)
        if not sources:
            detail = "; ".join(dict.fromkeys(errors)) or "no usable public search results"
            raise ValueError("DDGS found no usable web pages: " + detail)
        return {"query": query, "provider": "ddgs", "fetched_at": datetime.now(UTC).isoformat(),
                "sources": sources}

    def close(self) -> None:
        self.fetcher.close()
