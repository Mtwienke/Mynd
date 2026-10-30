from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from resolution.citations import CitationRecord
    from resolution.metadata import PaperMetadata


def insert_extracted_citations(
    database_path: Path, paper_id: str, citations: list[CitationRecord]
) -> None:
    """Persist GROBID references without replacing annotations already in SQLite."""
    with sqlite3.connect(database_path) as db:
        db.executemany(
            """INSERT OR IGNORE INTO citations
            (source_paper_id, target_paper_id, raw_reference, cited_doi, status, confidence, evidence)
            VALUES(?,?,?,?,?,?,?)""",
            [
                (
                    paper_id,
                    None,
                    citation["raw_reference"],
                    citation.get("doi"),
                    citation.get("status", "unresolved"),
                    citation.get("confidence", 0.0),
                    "GROBID bibliography extraction",
                )
                for citation in citations
            ],
        )


def citations_for_paper(database_path: Path, paper_id: str) -> list[CitationRecord]:
    """Return citation records joined with resolved paper note paths."""
    with sqlite3.connect(database_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT c.raw_reference, c.cited_doi AS doi, c.status,
                      target.note_path AS target_note
               FROM citations c LEFT JOIN papers target ON target.id=c.target_paper_id
               WHERE c.source_paper_id=? ORDER BY c.id""",
            (paper_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def document_for_digest(database_path: Path, digest: str) -> tuple | None:
    with sqlite3.connect(database_path) as db:
        return db.execute(
            "SELECT id, paper_id, stored_path FROM documents WHERE sha256=?", (digest,)
        ).fetchone()


def update_document_path(database_path: Path, document_id: str, path: Path) -> None:
    with sqlite3.connect(database_path) as db:
        db.execute("UPDATE documents SET stored_path=? WHERE id=?", (str(path), document_id))


def extracted_text_for_document(database_path: Path, document_id: str) -> str:
    with sqlite3.connect(database_path) as db:
        row = db.execute(
            "SELECT extracted_text FROM documents WHERE id=?", (document_id,)
        ).fetchone()
    return row[0] if row else ""


def metadata_for_paper(database_path: Path, paper_id: str) -> PaperMetadata | None:
    with sqlite3.connect(database_path) as db:
        row = db.execute(
            """SELECT doi, title, year, authors_json, venue, url, abstract, metadata_json
               FROM papers WHERE id=?""",
            (paper_id,),
        ).fetchone()
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


def paper_id_for_doi(database_path: Path, doi: str) -> str | None:
    with sqlite3.connect(database_path) as db:
        row = db.execute("SELECT id FROM papers WHERE lower(doi)=lower(?)", (doi,)).fetchone()
    return row[0] if row else None


def paper_for_doi(database_path: Path, doi: str) -> tuple[str, str | None] | None:
    with sqlite3.connect(database_path) as db:
        return db.execute(
            "SELECT id, note_path FROM papers WHERE lower(doi)=lower(?)", (doi,)
        ).fetchone()


def save_ingestion(
    database_path: Path,
    paper_id: str,
    metadata: PaperMetadata,
    digest: str,
    filename: str,
    stored_path: Path,
    extracted_text: str,
) -> None:
    fields = {"doi", "title", "year", "authors", "venue", "url", "abstract"}
    with sqlite3.connect(database_path) as db:
        db.execute(
            """INSERT INTO papers(id, doi, title, year, authors_json, venue, url, abstract, metadata_json)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 doi=COALESCE(excluded.doi, papers.doi),
                 title=CASE WHEN papers.title IS NULL OR papers.title='' THEN excluded.title ELSE papers.title END,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                paper_id,
                metadata.get("doi"),
                metadata.get("title") or Path(filename).stem,
                metadata.get("year"),
                json.dumps(metadata.get("authors") or []),
                metadata.get("venue"),
                metadata.get("url"),
                metadata.get("abstract"),
                json.dumps({key: value for key, value in metadata.items() if key not in fields}),
            ),
        )
        db.execute(
            """INSERT OR IGNORE INTO documents
               (id, paper_id, sha256, original_name, stored_path, extracted_text)
               VALUES(?,?,?,?,?,?)""",
            (digest[:24], paper_id, digest, filename, str(stored_path), extracted_text),
        )
        db.execute(
            "INSERT INTO ingestion_jobs(sha256, filename, status) VALUES(?,?,?)",
            (digest, filename, "ingested"),
        )


def set_paper_note_path(database_path: Path, paper_id: str, note_path: Path) -> None:
    with sqlite3.connect(database_path) as db:
        db.execute(
            "UPDATE papers SET note_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(note_path), paper_id),
        )


def insert_resolved_citations(
    database_path: Path, paper_id: str, citations: list[CitationRecord]
) -> None:
    with sqlite3.connect(database_path) as db:
        db.executemany(
            """INSERT OR IGNORE INTO citations
               (source_paper_id, target_paper_id, raw_reference, cited_doi, status, confidence, evidence)
               VALUES(?,?,?,?,?,?,?)""",
            [
                (
                    paper_id,
                    citation.get("target_id"),
                    citation["raw_reference"],
                    citation.get("doi"),
                    citation.get("status", "unresolved"),
                    citation.get("confidence", 0.0),
                    "GROBID bibliography extraction",
                )
                for citation in citations
            ],
        )


def record_ingestion_failure(
    database_path: Path, digest: str, filename: str, error: str
) -> None:
    with sqlite3.connect(database_path) as db:
        db.execute(
            "INSERT INTO ingestion_jobs(sha256, filename, status, error) VALUES(?,?,?,?)",
            (digest, filename, "failed", error),
        )


def unresolved_citations(database_path: Path) -> list[tuple]:
    with sqlite3.connect(database_path) as db:
        return db.execute(
            """SELECT p.title, c.raw_reference, c.cited_doi, c.status
               FROM citations c JOIN papers p ON p.id=c.source_paper_id
               WHERE c.status IN ('unresolved', 'ambiguous', 'external')
               ORDER BY p.title"""
        ).fetchall()
