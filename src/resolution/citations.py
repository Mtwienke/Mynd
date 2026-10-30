from pathlib import Path
from typing import Any, Required, TypedDict

import httpx

from resolution.crossref import CrossrefClient
from storage.repositories import paper_for_doi


class CitationRecord(TypedDict, total=False):
    """Dictionary contract shared by extraction, storage, summaries, and notes."""

    raw_reference: Required[str]
    doi: str | None
    target_id: str | None
    target_note: str | Path | None
    placeholder_note: str | Path | None
    status: str
    confidence: float
    evidence: str | None
    importance: str | None
    direction: str | None
    similarity: str | None


def resolve_extracted_references(
    references: list[dict[str, Any]], database_path: Path, crossref: CrossrefClient | None
) -> tuple[list[CitationRecord], list[str]]:
    """Resolve extracted DOIs against the local library and then Crossref."""
    citations: list[CitationRecord] = []
    errors: list[str] = []
    for reference in references:
        doi = reference.get("doi")
        target_id = None
        target_note = None
        status = "unresolved"
        confidence = 0.0
        if doi:
            target = paper_for_doi(database_path, doi)
            if target:
                target_id, target_note = target
                status, confidence = "resolved", 1.0
            elif crossref:
                try:
                    if crossref.by_doi(doi):
                        status, confidence = "external", 0.95
                except httpx.HTTPError as exc:
                    errors.append(f"{doi}: {exc}")
        citations.append({
            "raw_reference": reference["raw_reference"],
            "doi": doi,
            "target_id": target_id,
            "target_note": target_note,
            "status": status,
            "confidence": confidence,
        })
    return citations, errors
