"""SQLite persistence.

Outward-facing citation and metadata reads return mutable ``TypedDict``
contracts because callers add annotations before rendering. Internal row
projections use immutable ``NamedTuple`` or ``dataclass`` types so their
field order stays tied to the SQL that produces them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from mynd.contracts import CitationRecord, PaperMetadata
from mynd.storage.database import connect

_INSERT_CITATION_SQL = """\
INSERT OR IGNORE INTO citations
(source_paper_id, target_paper_id, raw_reference, cited_doi, status, confidence, evidence)
VALUES(?,?,?,?,?,?,?)"""

_CITATIONS_FOR_PAPER_SQL = """\
SELECT c.raw_reference, c.cited_doi AS doi, c.status, target.note_path AS target_note
FROM citations c LEFT JOIN papers target ON target.id=c.target_paper_id
WHERE c.source_paper_id=? ORDER BY c.id"""

_PAPER_METADATA_SQL = """\
SELECT doi, title, year, authors_json, venue, url, abstract, metadata_json
FROM papers WHERE id=?"""

_SAVE_PAPER_SQL = """\
INSERT INTO papers(id, doi, title, year, authors_json, venue, url, abstract, metadata_json)
VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
doi=COALESCE(excluded.doi, papers.doi),
title=CASE WHEN papers.title IS NULL OR papers.title='' THEN excluded.title ELSE papers.title END,
updated_at=CURRENT_TIMESTAMP"""

_INSERT_DOCUMENT_SQL = """\
INSERT OR IGNORE INTO documents
(id, paper_id, sha256, original_name, stored_path, extracted_text)
VALUES(?,?,?,?,?,?)"""

_UNRESOLVED_CITATIONS_SQL = """\
SELECT p.title, c.raw_reference, c.cited_doi, c.status
FROM citations c JOIN papers p ON p.id=c.source_paper_id
WHERE c.status IN ('unresolved', 'ambiguous', 'external')
ORDER BY p.title"""

_METADATA_FIELDS = {"doi", "title", "year", "authors", "venue", "url", "abstract"}
EXTRACTION_EVIDENCE = "GROBID bibliography extraction"


class StoredDocument(NamedTuple):
    """Row identifying an already-ingested PDF."""

    id: str
    paper_id: str
    stored_path: str


class PaperLocation(NamedTuple):
    """Local library hit for a DOI, including its rendered note path."""

    id: str
    note_path: str | None


@dataclass(frozen=True)
class IngestedDocument:
    """One PDF's identity, stored copy, and extracted text."""

    digest: str
    filename: str
    stored_path: Path
    text: str


class PendingCitation(NamedTuple):
    """Citation awaiting human review, joined with its source paper title."""

    title: str
    raw_reference: str
    doi: str | None
    status: str


def _citation_row(paper_id: str, citation: CitationRecord) -> tuple:
    """Project one citation record onto the persisted citation columns."""
    return (
        paper_id,
        citation.get("target_id"),
        citation["raw_reference"],
        citation.get("doi"),
        citation.get("status", "unresolved"),
        citation.get("confidence", 0.0),
        EXTRACTION_EVIDENCE,
    )


def insert_resolved_citations(
    database_path: Path, paper_id: str, citations: list[CitationRecord]
) -> None:
    """Persist resolved citations, ignoring references already stored."""
    rows = [_citation_row(paper_id, citation) for citation in citations]
    with connect(database_path) as db:
        db.executemany(_INSERT_CITATION_SQL, rows)


def citations_for_paper(database_path: Path, paper_id: str) -> list[CitationRecord]:
    """Return citation records joined with resolved paper note paths."""
    with connect(database_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(_CITATIONS_FOR_PAPER_SQL, (paper_id,)).fetchall()
    return [dict(row) for row in rows]


def document_for_digest(database_path: Path, digest: str) -> StoredDocument | None:
    """Return the stored document matching a PDF digest, if any."""
    with connect(database_path) as db:
        row = db.execute(
            "SELECT id, paper_id, stored_path FROM documents WHERE sha256=?", (digest,)
        ).fetchone()
    return StoredDocument(*row) if row else None


def update_document_path(database_path: Path, document_id: str, path: Path) -> None:
    """Record the new location of a document whose stored copy moved."""
    with connect(database_path) as db:
        db.execute(
            "UPDATE documents SET stored_path=? WHERE id=?", (str(path), document_id)
        )


def extracted_text_for_document(database_path: Path, document_id: str) -> str:
    """Return the extracted text saved with a document, or an empty string."""
    with connect(database_path) as db:
        row = db.execute(
            "SELECT extracted_text FROM documents WHERE id=?", (document_id,)
        ).fetchone()
    return row[0] if row else ""


def metadata_for_paper(database_path: Path, paper_id: str) -> PaperMetadata | None:
    """Return persisted bibliographic metadata for a paper."""
    with connect(database_path) as db:
        row = db.execute(_PAPER_METADATA_SQL, (paper_id,)).fetchone()
    if row is None:
        return None
    return {
        "doi": row[0],
        "title": row[1],
        "year": row[2],
        "authors": json.loads(row[3]),
        "venue": row[4],
        "url": row[5],
        "abstract": row[6],
        **json.loads(row[7]),
    }




def paper_for_doi(database_path: Path, doi: str) -> PaperLocation | None:
    """Return the paper id and note path stored for a DOI."""
    with connect(database_path) as db:
        row = db.execute(
            "SELECT id, note_path FROM papers WHERE lower(doi)=lower(?)", (doi,)
        ).fetchone()
    return PaperLocation(*row) if row else None


def save_ingestion(
    database_path: Path,
    paper_id: str,
    metadata: PaperMetadata,
    document: IngestedDocument,
) -> None:
    """Upsert a paper, its document, and its ingestion job in one transaction."""
    extras = {
        key: value for key, value in metadata.items() if key not in _METADATA_FIELDS
    }
    with connect(database_path) as db:
        db.execute(
            _SAVE_PAPER_SQL,
            (
                paper_id,
                metadata.get("doi"),
                metadata.get("title") or Path(document.filename).stem,
                metadata.get("year"),
                json.dumps(metadata.get("authors") or []),
                metadata.get("venue"),
                metadata.get("url"),
                metadata.get("abstract"),
                json.dumps(extras),
            ),
        )
        db.execute(
            _INSERT_DOCUMENT_SQL,
            (
                document.digest[:24],
                paper_id,
                document.digest,
                document.filename,
                str(document.stored_path),
                document.text,
            ),
        )
        db.execute(
            "INSERT INTO ingestion_jobs(sha256, filename, status) VALUES(?,?,?)",
            (document.digest, document.filename, "ingested"),
        )


def set_paper_note_path(database_path: Path, paper_id: str, note_path: Path) -> None:
    """Record the rendered note path for a paper."""
    with connect(database_path) as db:
        db.execute(
            "UPDATE papers SET note_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(note_path), paper_id),
        )


def record_ingestion_failure(
    database_path: Path, digest: str, filename: str, error: str
) -> None:
    """Record a failed ingestion attempt for later inspection."""
    with connect(database_path) as db:
        db.execute(
            "INSERT INTO ingestion_jobs(sha256, filename, status, error) VALUES(?,?,?,?)",
            (digest, filename, "failed", error),
        )


def unresolved_citations(database_path: Path) -> list[PendingCitation]:
    """Return citations that still need human review, with their source titles."""
    with connect(database_path) as db:
        rows = db.execute(_UNRESOLVED_CITATIONS_SQL).fetchall()
    return [PendingCitation(*row) for row in rows]
