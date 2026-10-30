from typing import Any
from urllib.parse import quote

import httpx

from mynd.contracts import PaperMetadata
from resolution.doi import normalize_doi

CROSSREF_API_URL = "https://api.crossref.org"


class CrossrefClient:
    """Shared Crossref REST client for ingestion and CLI workflows."""

    def __init__(self, email: str = "you@example.com", timeout: float = 15.0):
        self.client = httpx.Client(
            base_url=CROSSREF_API_URL,
            params={"mailto": email},
            timeout=timeout,
            headers={"User-Agent": "Mynd/0.1 (personal research library)"},
        )

    def by_doi(self, doi: str) -> dict[str, Any] | None:
        normalized = normalize_doi(doi)
        if normalized is None:
            return None
        response = self.client.get(f"/works/{quote(normalized, safe='')}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()["message"]

    def search_title(self, title: str) -> list[dict[str, Any]]:
        response = self.client.get("/works", params={"query.title": title, "rows": 5})
        response.raise_for_status()
        return response.json()["message"].get("items", [])

    def close(self) -> None:
        self.client.close()


def metadata_from_crossref(item: dict[str, Any]) -> PaperMetadata:
    authors = []
    for author in item.get("author", []):
        name = " ".join(part for part in (author.get("given"), author.get("family")) if part)
        if name:
            authors.append(name)
    date_parts = (
        item.get("published-print", {}).get("date-parts")
        or item.get("published-online", {}).get("date-parts")
        or item.get("issued", {}).get("date-parts")
        or []
    )
    titles = item.get("title") or []
    containers = item.get("container-title") or []
    return {
        "doi": normalize_doi(item.get("DOI")),
        "title": titles[0] if titles else None,
        "authors": authors,
        "year": date_parts[0][0] if date_parts and date_parts[0] else None,
        "venue": containers[0] if containers else None,
        "url": item.get("URL"),
        "abstract": item.get("abstract"),
        "type": item.get("type"),
    }
