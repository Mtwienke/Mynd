"""Official Wikipedia full-text search; results are URLs, never source snippets."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx

from mynd.connect.web import public_url
from mynd.connect.web_pages import USER_AGENT

API_URL = "https://en.wikipedia.org/w/api.php"
MAX_RESPONSE_BYTES = 512_000
_STOP_WORDS = {
    "the", "and", "for", "with", "from", "about", "what", "how", "why", "via",
    "definition", "overview",
}


def _title_match(term: str, title: str) -> float:
    """Prefer topic-like titles over biographies that merely mention the query."""
    def words(value):
        result = []
        for word in re.findall(r"[^\W\d_]+", value.casefold()):
            if len(word) < 3 or word in _STOP_WORDS:
                continue
            if len(word) > 4 and word.endswith("s"):
                word = word[:-1]
            if len(word) > 6 and word.endswith("ic"):
                word = word[:-2]
            result.append(word)
        return result

    query_words, title_words = set(words(term)), words(title)
    if not query_words or not title_words:
        return 0
    # A qualifier such as "(EDA)" can select a different sense of the same
    # words. Do not silently choose that sense when the public query omitted it.
    for qualifier in re.findall(r"\(([^)]+)\)", title):
        if not query_words.intersection(words(qualifier)):
            return 0
    initials = "".join(word[0] for word in title_words)
    if any(word.casefold() == initials for word in re.findall(r"\b[A-Z]{2,8}\b", term)):
        return 1
    overlap = query_words & set(title_words)
    if len(overlap) / len(query_words) < 0.5:
        return 0
    return len(overlap) / len(set(title_words))


class WikipediaSearchError(ValueError):
    """Safe search-stage diagnostics without remote bodies or credentials."""


class WikipediaSearch:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(20, connect=10), trust_env=False,
        )

    def search(self, query: str) -> list[dict]:
        term = re.sub(r"\s+definition\s+overview$", "", query.strip(), flags=re.IGNORECASE)
        if not term:
            return []
        params = {
            "action": "query", "format": "json", "formatversion": 2, "maxlag": 5,
            "generator": "search", "gsrsearch": term, "gsrnamespace": 0,
            "gsrlimit": 5, "gsrprop": "",
            "prop": "info|pageprops", "inprop": "url", "ppprop": "disambiguation",
        }
        try:
            with self.client.stream(
                "GET", API_URL, params=params, headers={"User-Agent": USER_AGENT},
                follow_redirects=False,
            ) as response:
                if response.status_code == 429:
                    raise WikipediaSearchError("Wikipedia API rate limited the request (HTTP 429); retry later")
                if not response.is_success:
                    raise WikipediaSearchError(
                        f"Wikipedia API returned HTTP {response.status_code}; check network access or retry later"
                    )
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=64_000):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise WikipediaSearchError("Wikipedia API response exceeded the size limit")
        except httpx.TimeoutException:
            raise WikipediaSearchError("Wikipedia API timed out; check network access or retry later") from None
        except httpx.HTTPError:
            raise WikipediaSearchError("Wikipedia API network request failed; check network access") from None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise WikipediaSearchError("Wikipedia API returned an invalid JSON response") from None
        if not isinstance(data, dict):
            raise WikipediaSearchError("Wikipedia API returned an invalid search response")
        if "error" in data:
            error = data["error"]
            code = error.get("code") if isinstance(error, dict) else ""
            if code in ("maxlag", "ratelimited"):
                raise WikipediaSearchError("Wikipedia API is temporarily busy or rate limited; retry later")
            raise WikipediaSearchError("Wikipedia API reported a search error")
        result = data.get("query", {})
        if not isinstance(result, dict):
            raise WikipediaSearchError("Wikipedia API returned invalid search results")
        pages = result.get("pages", [])
        if not isinstance(pages, list):
            raise WikipediaSearchError("Wikipedia API returned invalid search results")
        ranked = []
        for page in pages:
            if not isinstance(page, dict) or page.get("ns") != 0:
                continue
            properties = page.get("pageprops", {})
            if not isinstance(properties, dict):
                raise WikipediaSearchError("Wikipedia API returned invalid page metadata")
            if "missing" in page or "disambiguation" in properties:
                continue
            url, title = page.get("fullurl"), page.get("title")
            if (not isinstance(url, str) or not public_url(url)
                    or urlsplit(url).hostname != "en.wikipedia.org"
                    or not isinstance(title, str) or not title.strip()):
                continue
            index = page.get("index")
            if not isinstance(index, int):
                continue
            match = _title_match(term, title)
            if match:
                ranked.append((-match, index, {"href": url, "title": title}))
        if not ranked:
            return []
        best = min(row[0] for row in ranked)
        # Prefer one close reference over filling the overview with loosely
        # related biographies, lists, or narrower variants from the full text.
        return [item for score, _, item in sorted(ranked, key=lambda row: row[:2])
                if score == best][:5]

    def close(self) -> None:
        self.client.close()
