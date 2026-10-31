from collections.abc import Callable

import httpx

from mynd.contracts import CitationRecord, ReferenceRecord
from mynd.resolution.crossref import CrossrefClient


def resolve_extracted_references(
    references: list[ReferenceRecord],
    local_lookup: Callable[[str], tuple[str, str | None] | None],
    crossref: CrossrefClient | None,
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
            target = local_lookup(doi)
            if target:
                target_id, target_note = target
                status, confidence = "resolved", 1.0
            elif crossref:
                try:
                    if crossref.by_doi(doi):
                        status, confidence = "external", 0.95
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    errors.append(f"{doi}: {exc}")
        citations.append({
            "raw_reference": reference["raw_reference"],
            "doi": doi,
            "title": reference.get("title"),
            "target_id": target_id,
            "target_note": target_note,
            "status": status,
            "confidence": confidence,
        })
    return citations, errors
