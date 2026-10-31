import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from mynd.core import ingest_inbox, ingest_pdf
from mynd.settings import load_settings
from mynd.storage.database import initialize_database


@pytest.fixture
def mock_settings(tmp_path):
    # Create a dummy config file
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'vault_path = "{tmp_path / "vault"}"\n'
        f'data_path = "{tmp_path / "data"}"\n'
        f'inbox_path = "{tmp_path / "inbox"}"\n'
        f'pdf_store_path = "{tmp_path / "pdf_store"}"\n'
        f'database_path = "{tmp_path / "mynd.sqlite3"}"\n'
        f"use_grobid = false\n"
    )
    settings = load_settings(config_file)
    initialize_database(settings.database_path)
    return settings


@pytest.fixture
def sample_pdf(tmp_path):
    pdf = tmp_path / "test_paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 test content")
    return pdf


def test_ingest_pdf_success(mock_settings, sample_pdf):
    # Mocking expensive/external components
    with (
        patch(
            "mynd.core.extract_text_and_doi",
            return_value=("This is the paper text", "10.1234/test"),
        ),
        patch("mynd.core.write_paper_note", return_value=Path("vault/note.md")),
        patch("mynd.core.sha256_file", return_value="fake-sha256"),
        patch("mynd.core.repositories.paper_for_doi", return_value=None),
    ):
        result = ingest_pdf(sample_pdf, mock_settings)

        assert result["status"] == "ingested"
        assert result["doi"] == "10.1234/test"
        assert "paper_id" in result
        assert result["note"] == Path("vault/note.md")


def test_ingest_pdf_already_ingested(mock_settings, sample_pdf):
    # First ingestion
    with (
        patch("mynd.core.extract_text_and_doi", return_value=("Text", "10.1234/test")),
        patch("mynd.core.write_paper_note", return_value=Path("vault/note.md")),
        patch("mynd.core.sha256_file", return_value="fake-sha256"),
    ):
        ingest_pdf(sample_pdf, mock_settings)

        # Second ingestion of the same file
        result = ingest_pdf(sample_pdf, mock_settings)

        assert result["status"] == "already_ingested"


def test_ingest_pdf_records_metadata_lookup_error(mock_settings, sample_pdf):
    crossref = MagicMock()
    crossref.by_doi.side_effect = httpx.HTTPError("crossref down")
    with (
        patch("mynd.core.extract_text_and_doi", return_value=("Text", "10.1234/test")),
        patch("mynd.core.write_paper_note", return_value=Path("vault/note.md")),
        patch("mynd.core.sha256_file", return_value="digest-error"),
    ):
        result = ingest_pdf(sample_pdf, mock_settings, crossref)

    assert result["status"] == "ingested"
    assert result["metadata_error"] == "crossref down"


def test_ingest_inbox_workflow(mock_settings, tmp_path):
    inbox = mock_settings.inbox_path
    inbox.mkdir(parents=True, exist_ok=True)

    # Create two sample PDFs
    (inbox / "paper1.pdf").write_bytes(b"pdf1")
    (inbox / "paper2.pdf").write_bytes(b"pdf2")

    with (
        patch("mynd.core.ingest_pdf") as mock_ingest,
        patch("mynd.core.build_text_generator"),
        patch("mynd.core.RuntimeControl"),
    ):
        mock_ingest.return_value = {
            "status": "ingested",
            "paper_id": "123",
            "doi": "doi",
            "title": "title",
            "note": Path("n"),
            "references_extracted": 0,
            "extracted_text": Path("t"),
        }

        results = ingest_inbox(mock_settings)

        assert len(results) == 2
        assert mock_ingest.call_count == 2
        assert all(r["status"] == "ingested" for r in results)


def test_ingest_pdf_invalid_file(mock_settings, tmp_path):
    txt_file = tmp_path / "not_a_pdf.txt"
    txt_file.write_text("I am not a PDF")

    with pytest.raises(ValueError, match="Not a PDF file"):
        ingest_pdf(txt_file, mock_settings)


def test_ingest_inbox_isolates_per_document_failure(mock_settings):
    inbox = mock_settings.inbox_path
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "a.pdf").write_bytes(b"a")
    (inbox / "b.pdf").write_bytes(b"b")
    success = {
        "status": "ingested",
        "paper_id": "p",
        "doi": None,
        "title": "t",
        "note": Path("n"),
        "references_extracted": 0,
        "extracted_text": Path("t"),
    }

    def fake_ingest(pdf, *args, **kwargs):
        if pdf.name == "b.pdf":
            raise ValueError("boom")
        return dict(success)

    with (
        patch("mynd.core.ingest_pdf", side_effect=fake_ingest),
        patch("mynd.core.build_text_generator"),
        patch("mynd.core.RuntimeControl"),
    ):
        results = ingest_inbox(mock_settings)

    assert [result["status"] for result in results] == ["ingested", "failed"]
    assert results[1]["filename"] == "b.pdf"


def test_record_failure_tolerates_missing_pdf(mock_settings, tmp_path):
    from mynd.core import _record_failure

    _record_failure(mock_settings, tmp_path / "gone.pdf", ValueError("vanished"))

    with sqlite3.connect(mock_settings.database_path) as db:
        row = db.execute(
            "SELECT sha256, filename, status FROM ingestion_jobs ORDER BY id DESC"
        ).fetchone()
    assert row == ("", "gone.pdf", "failed")


def test_ingest_pdf_runs_summarizer_for_extracted_text(mock_settings, sample_pdf):
    summarizer = MagicMock()
    summarizer.summarize.return_value = "A short summary."
    summarizer.annotate_references.side_effect = lambda text, citations, metadata: citations

    with (
        patch("mynd.core.extract_text_and_doi", return_value=("Body text", "10.1234/x")),
        patch("mynd.core.write_paper_note", return_value=Path("note.md")),
        patch("mynd.core.sha256_file", return_value="digest-summary"),
        patch("mynd.core.repositories.paper_for_doi", return_value=None),
    ):
        result = ingest_pdf(sample_pdf, mock_settings, summarizer=summarizer)

    summarizer.summarize.assert_called_once()
    summarizer.annotate_references.assert_called_once()
    assert result["status"] == "ingested"
    assert result["note"] == Path("note.md")


def test_ingest_pdf_refuses_non_local_grobid_egress(mock_settings, sample_pdf):
    remote = replace(
        mock_settings,
        use_grobid=True,
        grobid_url="https://grobid.example.com",
    )

    with (
        patch("mynd.core.extract_text_and_doi", return_value=("Body text", "10.1234/x")),
        patch("mynd.core.write_paper_note", return_value=Path("note.md")),
        patch("mynd.core.sha256_file", return_value="digest-egress"),
        patch("mynd.core.repositories.paper_for_doi", return_value=None),
    ):
        result = ingest_pdf(sample_pdf, remote)

    assert "Refusing to send PDFs" in result["references_error"]
