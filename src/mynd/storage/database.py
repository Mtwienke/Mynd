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
                grobid_json TEXT NOT NULL DEFAULT '{}',
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
            CREATE TABLE IF NOT EXISTS courses (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                note_path TEXT NOT NULL,
                university TEXT NOT NULL DEFAULT '',
                semester TEXT NOT NULL DEFAULT '',
                lecturer TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                sections_json TEXT NOT NULL DEFAULT '[]',
                questions_json TEXT NOT NULL DEFAULT '[]',
                fingerprint TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS lectures (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id),
                title TEXT NOT NULL,
                language TEXT NOT NULL,
                order_key TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                original_path TEXT NOT NULL,
                pdf_path TEXT NOT NULL,
                note_path TEXT NOT NULL,
                pages_json TEXT NOT NULL,
                sections_json TEXT NOT NULL DEFAULT '[]',
                questions_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL,
                proposal_json TEXT NOT NULL DEFAULT '{}',
                pdf_sha256 TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS lectures_digest ON lectures(sha256);
            CREATE TABLE IF NOT EXISTS lecture_sources (
                source_path TEXT PRIMARY KEY,
                lecture_id TEXT NOT NULL REFERENCES lectures(id)
            );
            CREATE TABLE IF NOT EXISTS course_ingestion_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        _ensure_citation_columns(db)
        _ensure_course_columns(db)


def _ensure_course_columns(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(lectures)")}
    if "pdf_sha256" not in columns:
        connection.execute("ALTER TABLE lectures ADD COLUMN pdf_sha256 TEXT NOT NULL DEFAULT ''")


def _ensure_citation_columns(connection: sqlite3.Connection) -> None:
    """Add columns introduced after a library database was first created."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(citations)").fetchall()
    }
    if "grobid_json" not in columns:
        connection.execute(
            "ALTER TABLE citations ADD COLUMN grobid_json TEXT NOT NULL DEFAULT '{}'"
        )
