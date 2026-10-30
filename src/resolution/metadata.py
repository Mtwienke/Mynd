from typing import Required, TypedDict


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


class ReferenceRecord(TypedDict, total=False):
    """One bibliography entry extracted from a PDF, before resolution."""

    raw_reference: Required[str]
    doi: str | None
    title: str | None
