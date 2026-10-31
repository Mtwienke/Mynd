import sqlite3
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection that enforces the schema's declared foreign keys."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(path: Path) -> None:
    """Create the local research library schema if it does not exist yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS papers (
                id TEXT PRIMARY KEY,
                doi TEXT UNIQUE,
                title TEXT NOT NULL,
                year INTEGER,
                authors_json TEXT NOT NULL DEFAULT '[]',
                venue TEXT,
                url TEXT,
                abstract TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                note_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                paper_id TEXT,
                sha256 TEXT NOT NULL UNIQUE,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                extracted_text TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(paper_id) REFERENCES papers(id)
            );
            CREATE TABLE IF NOT EXISTS citations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_paper_id TEXT NOT NULL,
                target_paper_id TEXT,
                raw_reference TEXT NOT NULL,
                cited_doi TEXT,
                status TEXT NOT NULL DEFAULT 'unresolved',
                confidence REAL NOT NULL DEFAULT 0,
                evidence TEXT,
                FOREIGN KEY(source_paper_id) REFERENCES papers(id),
                FOREIGN KEY(target_paper_id) REFERENCES papers(id),
                UNIQUE(source_paper_id, raw_reference)
            );
            CREATE TABLE IF NOT EXISTS ingestion_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
