import sqlite3

from mynd.storage.database import initialize_database


def _tables(path):
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return {row[0] for row in rows}


def test_initialize_database_creates_expected_tables(tmp_path):
    db_path = tmp_path / "nested" / "library.sqlite3"
    initialize_database(db_path)

    assert db_path.exists()
    assert _tables(db_path) >= {
        "papers",
        "documents",
        "citations",
        "ingestion_jobs",
    }


def test_initialize_database_is_idempotent(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    initialize_database(db_path)
    initialize_database(db_path)
    assert "papers" in _tables(db_path)


def test_initialize_database_schema_has_expected_columns(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    initialize_database(db_path)
    with sqlite3.connect(db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(papers)")}
    assert {"id", "doi", "title", "authors_json", "note_path"} <= columns
