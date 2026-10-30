from unittest.mock import MagicMock, patch

import pymupdf

from ingestion.extract_references import extract_grobid_references
from ingestion.extract_text import extract_text_and_doi


def _write_pdf(path, text_lines):
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in text_lines:
        page.insert_text((72, y), line)
        y += 18
    doc.save(path)
    doc.close()


def test_extract_text_and_doi_returns_text_and_doi(tmp_path):
    pdf = tmp_path / "paper.pdf"
    _write_pdf(pdf, ["A title", "DOI: 10.1234/extracted"])
    text, doi = extract_text_and_doi(pdf)
    assert "A title" in text
    assert doi == "10.1234/extracted"


def test_extract_text_and_doi_without_doi(tmp_path):
    pdf = tmp_path / "paper.pdf"
    _write_pdf(pdf, ["No identifier here"])
    text, doi = extract_text_and_doi(pdf)
    assert "No identifier" in text
    assert doi is None


GROBID_XML = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <text>
    <back>
      <div>
        <listBibl>
          <biblStruct>
            <analytic>
              <title>A cited paper</title>
            </analytic>
            <idno type="DOI">10.5555/cited</idno>
          </biblStruct>
          <biblStruct>
            <analytic>
              <title>No DOI reference</title>
            </analytic>
          </biblStruct>
        </listBibl>
      </div>
    </back>
  </text>
</TEI>
"""


def test_extract_grobid_references_parses_tei(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    response = MagicMock()
    response.text = GROBID_XML
    response.raise_for_status.return_value = None

    with patch("ingestion.extract_references.httpx.post", return_value=response) as post:
        references = extract_grobid_references(pdf, base_url="http://localhost:8070")

    assert len(references) == 2
    assert references[0]["doi"] == "10.5555/cited"
    assert references[0]["title"] == "A cited paper"
    assert references[1]["doi"] is None
    assert references[1]["title"] == "No DOI reference"
    assert post.call_args.kwargs["timeout"] == 90.0


def test_extract_grobid_references_empty_bibliography(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    response = MagicMock()
    response.text = '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text/></TEI>'
    response.raise_for_status.return_value = None

    with patch("ingestion.extract_references.httpx.post", return_value=response):
        assert extract_grobid_references(pdf, base_url="http://localhost:8070") == []
