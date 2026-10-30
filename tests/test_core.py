import hashlib
import sqlite3
import pymupdf

from mynd.core import (
    extract_pdf,
    init_db,
    normalize_doi,
    sha256_file,
    write_note,
)


def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1234/ABC.def") == "10.1234/ABC.def"
    assert normalize_doi("doi: 10.5555/example-1.") == "10.5555/example-1"
    assert normalize_doi("not a DOI") is None


def test_sha256_file(tmp_path):
    file = tmp_path / "paper.pdf"
    file.write_bytes(b"sample")
    assert sha256_file(file) == hashlib.sha256(b"sample").hexdigest()


def test_extract_pdf_finds_doi(tmp_path):
    path = tmp_path / "sample.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Example paper. DOI: 10.1234/test-paper")
    doc.save(path)
    doc.close()

    text, doi = extract_pdf(path)
    assert "Example paper" in text
    assert doi == "10.1234/test-paper"


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "data.sqlite3"
    init_db(db_path)
    init_db(db_path)
    with sqlite3.connect(db_path) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"papers", "documents", "citations", "ingestion_jobs"} <= tables


def test_write_note_preserves_human_section(tmp_path):
    vault = tmp_path / "vault"
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"placeholder")
    metadata = {
        "title": "Test Paper", "doi": "10.1234/test", "year": 2024,
        "authors": ["A Author"], "venue": "Journal", "url": "https://example.org",
        "abstract": "An abstract.",
    }
    note = write_note(vault, "doi-abcdef123456", metadata, source)
    original = note.read_text(encoding="utf-8").replace(
        "_Add your own interpretation here. This section is preserved during note updates._",
        "My important interpretation."
    )
    note.write_text(original, encoding="utf-8")

    write_note(vault, "doi-abcdef123456", metadata, source)
    updated = note.read_text(encoding="utf-8")
    assert "My important interpretation." in updated
    assert "## References" in updated
