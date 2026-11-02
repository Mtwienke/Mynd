"""Shared data contracts used across extraction, storage, rendering, and the CLI."""

from pathlib import Path
from typing import Literal, NotRequired, Required, TypedDict

CitationStatus = Literal["resolved", "external", "unresolved", "ambiguous"]
InferenceMode = Literal["normal", "silent"]


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


class GrobidReference(TypedDict, total=False):
    """Everything GROBID reported for one bibliography entry."""

    title: str | None
    authors: list[str]
    year: int | None
    venue: str | None
    volume: str | None
    issue: str | None
    pages: str | None
    publisher: str | None
    url: str | None
    doi: str | None
    identifiers: dict[str, str]
    raw_reference: str
    tei: str


class ReferenceRecord(TypedDict, total=False):
    """One bibliography entry extracted from a PDF, before resolution."""

    raw_reference: Required[str]
    doi: str | None
    title: str | None
    authors: list[str]
    year: int | None
    venue: str | None
    volume: str | None
    issue: str | None
    pages: str | None
    publisher: str | None
    url: str | None
    grobid: GrobidReference


class CitationRecord(TypedDict, total=False):
    """Dictionary contract shared by extraction, storage, summaries, and notes."""

    raw_reference: Required[str]
    doi: str | None
    title: str | None
    authors: list[str]
    year: int | None
    venue: str | None
    volume: str | None
    issue: str | None
    pages: str | None
    publisher: str | None
    url: str | None
    grobid: GrobidReference
    target_id: str | None
    target_note: str | Path | None
    placeholder_note: str | Path | None
    status: CitationStatus
    confidence: float
    evidence: str | None
    importance: str | None
    direction: str | None
    similarity: str | None


class IngestionSuccess(TypedDict):
    """Result of ingesting one PDF that reached the note-writing stage."""

    status: str
    paper_id: str
    doi: str | None
    title: str | None
    note: Path
    references_extracted: int
    extracted_text: Path
    metadata_error: NotRequired[str]
    references_error: NotRequired[str]
    citation_errors: NotRequired[list[str]]


class IngestionFailure(TypedDict):
    """Result for one PDF whose ingestion raised and was isolated."""

    status: str
    filename: str
    error: str


IngestionResult = IngestionSuccess | IngestionFailure
