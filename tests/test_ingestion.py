from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from mynd.core import ingest_inbox, ingest_pdf
from mynd.settings import load_settings


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
        f'use_grobid = false\n'
    )
    return load_settings(config_file)

@pytest.fixture
def sample_pdf(tmp_path):
    pdf = tmp_path / "test_paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 test content")
    return pdf

def test_ingest_pdf_success(mock_settings, sample_pdf):
    """Test the full ingestion pipeline with mocked external dependencies."""
    # Mocking expensive/external components
    with patch("mynd.core.extract_pdf", return_value=("This is the paper text", "10.1234/test")), \
         patch("mynd.core.write_paper_note", return_value=Path("vault/note.md")), \
         patch("mynd.core.sha256_file", return_value="fake-sha256"), \
         patch("storage.repositories.paper_for_doi", return_value=None):
        
        result = ingest_pdf(sample_pdf, mock_settings)
        
        assert result["status"] == "ingested"
        assert result["doi"] == "10.1234/test"
        assert "paper_id" in result
        assert result["note"] == Path("vault/note.md")

def test_ingest_pdf_already_ingested(mock_settings, sample_pdf):
    """Test that already ingested PDFs are handled correctly (refresh path)."""
    # First ingestion
    with patch("mynd.core.extract_pdf", return_value=("Text", "10.1234/test")), \
         patch("mynd.core.write_paper_note", return_value=Path("vault/note.md")), \
         patch("mynd.core.sha256_file", return_value="fake-sha256"):
        
        ingest_pdf(sample_pdf, mock_settings)
        
        # Second ingestion of the same file
        result = ingest_pdf(sample_pdf, mock_settings)
        
        assert result["status"] == "already_ingested"

def test_ingest_pdf_records_metadata_lookup_error(mock_settings, sample_pdf):
    """A Crossref outage should be reported without aborting local ingestion."""
    crossref = MagicMock()
    crossref.by_doi.side_effect = httpx.HTTPError("crossref down")
    with patch("mynd.core.extract_pdf", return_value=("Text", "10.1234/test")), \
         patch("mynd.core.write_paper_note", return_value=Path("vault/note.md")), \
         patch("mynd.core.sha256_file", return_value="digest-error"):

        result = ingest_pdf(sample_pdf, mock_settings, crossref)

    assert result["status"] == "ingested"
    assert result["metadata_error"] == "crossref down"


def test_ingest_inbox_workflow(mock_settings, tmp_path):
    """Test that ingest_inbox discovers PDFs and calls ingest_pdf."""
    inbox = mock_settings.inbox_path
    inbox.mkdir(parents=True, exist_ok=True)
    
    # Create two sample PDFs
    (inbox / "paper1.pdf").write_bytes(b"pdf1")
    (inbox / "paper2.pdf").write_bytes(b"pdf2")
    
    with patch("mynd.core.ingest_pdf") as mock_ingest, \
         patch("mynd.core.get_model_manager"), \
         patch("mynd.core.RuntimeControl"):
        
        mock_ingest.return_value = {"status": "ingested", "paper_id": "123", "doi": "doi", "title": "title", "note": Path("n"), "references_extracted": 0, "extracted_text": Path("t")}
        
        results = ingest_inbox(mock_settings)
        
        assert len(results) == 2
        assert mock_ingest.call_count == 2
        assert all(r["status"] == "ingested" for r in results)

def test_ingest_pdf_invalid_file(mock_settings, tmp_path):
    """Test that non-PDF files raise ValueError."""
    txt_file = tmp_path / "not_a_pdf.txt"
    txt_file.write_text("I am not a PDF")
    
    with pytest.raises(ValueError, match="Not a PDF file"):
        ingest_pdf(txt_file, mock_settings)
