"""Key-free search via DDGS and bounded local page extraction."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx

from mynd.connect.web import public_url
from mynd.connect.web_pages import PageFetcher, PageFetchError
from mynd.connect.web_wikipedia import WikipediaSearch, WikipediaSearchError
from mynd.vault.safety import digest

# Explicit engines avoid sending topic queries to every backend DDGS supports.
SEARCH_BACKENDS = "duckduckgo,brave,mojeek"


class DDGSWeb:
    def __init__(
        self, fetcher: PageFetcher | None = None, references: WikipediaSearch | None = None,
    ):
        self.fetcher = fetcher or PageFetcher()
        self._references = references
        self._search_error = ""

    def _search(self, query: str) -> list[dict]:
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

        try:
            with DDGS(timeout=10) as search:
                return search.text(query, max_results=5, backend=SEARCH_BACKENDS)
        except DDGSException as exc:
            # A base DDGSException also means "No results found", not necessarily
            # throttling. Never echo its arbitrary remote response/body text.
            if isinstance(exc, RatelimitException):
                self._search_error = "rate limited"
            elif isinstance(exc, TimeoutException):
                self._search_error = "timed out"
            else:
                self._search_error = "no results or engine refusal (DDGSException)"
            return []

    def _reference_search(self, query: str) -> list[dict]:
        if self._references is None:
            self._references = WikipediaSearch()
        return self._references.search(query)

    def _fetch_sources(self, results: list[dict]) -> tuple[list[dict], list[str]]:
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
        return sources, errors

    def research(self, query: str) -> dict:
        self._search_error = ""
        results = self._search(query)
        if not isinstance(results, list):
            self._search_error = "invalid DDGS result list"
            results = []
        sources, errors = self._fetch_sources(results)
        backend = "general web search"
        if not sources:
            general = self._search_error or "; ".join(dict.fromkeys(errors)) or "no usable public search results"
            try:
                references = self._reference_search(query)
            except WikipediaSearchError as exc:
                raise ValueError(f"General search: {general}. {exc}") from None
            sources, reference_errors = self._fetch_sources(references)
            backend = "Wikipedia full-text API"
            if not sources:
                detail = "; ".join(dict.fromkeys(reference_errors))
                if references:
                    reason = "Wikipedia found articles, but no usable pages were fetched: " + detail
                else:
                    reason = (
                        "Wikipedia full-text search found no matching articles (not a rate-limit error). "
                        "Use a shorter, established public term with --web-query TOPIC_ID=QUERY"
                    )
                raise ValueError(f"General search: {general}. {reason}")
        return {"query": query, "provider": "ddgs", "fetched_at": datetime.now(UTC).isoformat(),
                "search_backend": backend, "sources": sources}

    def close(self) -> None:
        try:
            self.fetcher.close()
        finally:
            if self._references is not None:
                self._references.close()
