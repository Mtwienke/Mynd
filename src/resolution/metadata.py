from typing import TypedDict


class PaperMetadata(TypedDict, total=False):
    """Bibliographic metadata shared across lookup, persistence, and rendering."""

    doi: str | None
    title: str | None
    authors: list[str]
    year: int | None
    venue: str | None
    url: str | None
    abstract: str | None
    type: str | None
