from unittest.mock import MagicMock, patch

import pymupdf

from mynd.ingestion.extract_references import extract_grobid_references
from mynd.ingestion.extract_text import extract_text_and_doi


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
              <title level="a" type="main">A cited paper</title>
              <author>
                <persName><forename>Ada</forename><surname>Lovelace</surname></persName>
              </author>
              <author>
                <persName><forename>Alan</forename><surname>Turing</surname></persName>
              </author>
              <idno type="DOI">10.5555/cited</idno>
              <idno type="arXiv">2401.00001</idno>
            </analytic>
            <monogr>
              <title level="j">Journal of Examples</title>
              <imprint>
                <biblScope unit="volume">12</biblScope>
                <biblScope unit="issue">3</biblScope>
                <biblScope unit="page" from="10" to="20">10-20</biblScope>
                <date type="published" when="2023-05-01">2023</date>
                <publisher>Example Press</publisher>
              </imprint>
            </monogr>
            <ptr target="https://example.org/cited"/>
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

    with patch("mynd.ingestion.extract_references.httpx.post", return_value=response) as post:
        references = extract_grobid_references(pdf, base_url="http://localhost:8070")

    assert len(references) == 2
    first = references[0]
    assert first["doi"] == "10.5555/cited"
    assert first["title"] == "A cited paper"
    assert first["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert first["year"] == 2023
    assert first["venue"] == "Journal of Examples"
    assert first["volume"] == "12"
    assert first["issue"] == "3"
    assert first["pages"] == "10-20"
    assert first["publisher"] == "Example Press"
    assert first["url"] == "https://example.org/cited"
    assert first["grobid"]["identifiers"] == {
        "doi": "10.5555/cited",
        "arxiv": "2401.00001",
    }
    assert "A cited paper" in first["grobid"]["tei"]
    assert "biblStruct" in first["grobid"]["tei"]
    assert "ns0:" not in first["grobid"]["tei"]
    assert references[1]["doi"] is None
    assert references[1]["title"] == "No DOI reference"
    assert post.call_args.kwargs["timeout"] == 90.0


def test_extract_grobid_references_empty_bibliography(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    response = MagicMock()
    response.text = '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text/></TEI>'
    response.raise_for_status.return_value = None

    with patch("mynd.ingestion.extract_references.httpx.post", return_value=response):
        assert extract_grobid_references(pdf, base_url="http://localhost:8070") == []
