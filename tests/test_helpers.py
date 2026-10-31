import hashlib
import sqlite3

import pymupdf

from ingestion.extract_text import extract_text_and_doi
from ingestion.text import save_extracted_text, text_without_references
from mynd.files import sha256_file
from mynd.settings import load_settings
from resolution.doi import normalize_doi
from storage.database import initialize_database
from storage.repositories import IngestedDocument, insert_resolved_citations, save_ingestion
from vault.note_writer import PaperNote, write_paper_note
from vault.review_queue import write_review_queue


def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1234/ABC.def") == "10.1234/ABC.def"
    assert normalize_doi("doi: 10.5555/example-1.") == "10.5555/example-1"
    assert normalize_doi("not a DOI") is None


def test_sha256_file(tmp_path):
    file = tmp_path / "paper.pdf"
    file.write_bytes(b"sample")
    assert sha256_file(file) == hashlib.sha256(b"sample").hexdigest()


def test_save_extracted_text_uses_pdf_stem(tmp_path):
    source = tmp_path / "stored" / "abc123.pdf"
    output = save_extracted_text(tmp_path / "data", source, "full text")
    assert output == tmp_path / "data" / "extracted_text" / "abc123.txt"
    assert output.read_text(encoding="utf-8") == "full text"
    assert not output.with_name("abc123.txt.tmp").exists()


def test_text_without_references_trims_terminal_bibliography():
    body = "A" * 100
    text = f"{body}\n\nREFERENCES\n[1] A citation"
    assert text_without_references(text) == body


def test_text_without_references_keeps_text_without_heading():
    text = "Intro only, no bibliography here."
    assert text_without_references(text) == text


def test_load_settings_resolves_paths_relative_to_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        'vault_path = "vault"\n'
        'data_path = "data"\n'
        'inbox_path = "inbox"\n'
        'pdf_store_path = "pdfs"\n'
        'database_path = "library.sqlite3"\n'
        "grobid_url = \"http://localhost:8070/\"\n"
        "use_grobid = false\n",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.vault_path == tmp_path / "vault"
    assert settings.database_path == tmp_path / "library.sqlite3"
    assert settings.grobid_url == "http://localhost:8070"
    assert settings.use_grobid is False


def test_write_review_queue_lists_unresolved_citations(tmp_path):
    settings = load_settings(_write_config(tmp_path))
    initialize_database(settings.database_path)
    save_ingestion(
        settings.database_path,
        "doi-paper",
        {"title": "Source Paper", "authors": []},
        IngestedDocument(
            digest="digest",
            filename="source.pdf",
            stored_path=tmp_path / "source.pdf",
            text="text",
        ),
    )
    insert_resolved_citations(
        settings.database_path,
        "doi-paper",
        [{"raw_reference": "Missing work", "doi": "10.9/missing", "status": "unresolved"}],
    )

    output = write_review_queue(settings)

    content = output.read_text(encoding="utf-8")
    assert "Source Paper" in content
    assert "Missing work" in content
    assert "10.9/missing" in content


def test_write_review_queue_handles_empty_queue(tmp_path):
    settings = load_settings(_write_config(tmp_path))
    output = write_review_queue(settings)
    assert "_No pending citation records._" in output.read_text(encoding="utf-8")


def _write_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        f'vault_path = "{(tmp_path / "vault").as_posix()}"\n'
        f'data_path = "{(tmp_path / "data").as_posix()}"\n'
        f'inbox_path = "{(tmp_path / "inbox").as_posix()}"\n'
        f'pdf_store_path = "{(tmp_path / "pdfs").as_posix()}"\n'
        f'database_path = "{(tmp_path / "library.sqlite3").as_posix()}"\n'
        "use_grobid = false\n",
        encoding="utf-8",
    )
    return config


def test_extract_text_and_doi_finds_doi(tmp_path):
    path = tmp_path / "sample.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Example paper. DOI: 10.1234/test-paper")
    doc.save(path)
    doc.close()

    text, doi = extract_text_and_doi(path)
    assert "Example paper" in text
    assert doi == "10.1234/test-paper"


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "data.sqlite3"
    initialize_database(db_path)
    initialize_database(db_path)
    with sqlite3.connect(db_path) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"papers", "documents", "citations", "ingestion_jobs"} <= tables


def test_write_paper_note_preserves_human_section(tmp_path):
    vault = tmp_path / "vault"
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"placeholder")
    metadata = {
        "title": "Test Paper", "doi": "10.1234/test", "year": 2024,
        "authors": ["A Author"], "venue": "Journal", "url": "https://example.org",
        "abstract": "An abstract.",
    }
    note = write_paper_note(PaperNote(vault, "doi-abcdef123456", metadata, source))
    original = note.read_text(encoding="utf-8").replace(
        "_Add your own interpretation here. This section is preserved during note updates._",
        "My important interpretation."
    )
    note.write_text(original, encoding="utf-8")

    write_paper_note(PaperNote(vault, "doi-abcdef123456", metadata, source))
    updated = note.read_text(encoding="utf-8")
    assert "My important interpretation." in updated
    assert "## References" in updated
