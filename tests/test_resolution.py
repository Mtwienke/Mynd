from unittest.mock import MagicMock

import httpx

from mynd.resolution.citations import resolve_extracted_references
from mynd.resolution.crossref import CrossrefClient, metadata_from_crossref
from mynd.resolution.doi import normalize_doi


def test_normalize_doi_url_and_text():
    assert normalize_doi("https://doi.org/10.1234/ABC.def") == "10.1234/ABC.def"
    assert normalize_doi("dx.doi.org/10.5555/example-1") == "10.5555/example-1"
    assert normalize_doi("see 10.1000/xyz123.") == "10.1000/xyz123"
    assert normalize_doi(None) is None
    assert normalize_doi("no doi here") is None


def _crossref_client() -> CrossrefClient:
    return CrossrefClient(timeout=1.0)


def test_crossref_by_doi_returns_message():
    client = _crossref_client()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"message": {"DOI": "10.1234/test"}}
    client.client = MagicMock()
    client.client.get.return_value = response

    result = client.by_doi("10.1234/test")
    assert result == {"DOI": "10.1234/test"}
    client.client.get.assert_called_once()


def test_crossref_by_doi_404_returns_none():
    client = _crossref_client()
    response = MagicMock()
    response.status_code = 404
    client.client = MagicMock()
    client.client.get.return_value = response

    assert client.by_doi("10.1234/missing") is None


def test_crossref_by_doi_invalid_returns_none():
    client = _crossref_client()
    client.client = MagicMock()
    assert client.by_doi("not-a-doi") is None
    client.client.get.assert_not_called()


def test_crossref_search_title_returns_items():
    client = _crossref_client()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"message": {"items": [{"DOI": "10.1/a"}]}}
    client.client = MagicMock()
    client.client.get.return_value = response

    assert client.search_title("Example") == [{"DOI": "10.1/a"}]


def test_metadata_from_crossref_full_item():
    item = {
        "DOI": "https://doi.org/10.1234/Test",
        "title": ["A Paper"],
        "author": [
            {"given": "Ada", "family": "Lovelace"},
            {"given": "Alan", "family": "Turing"},
            {"given": "OnlyGiven"},
        ],
        "published-print": {"date-parts": [[2020, 1, 2]]},
        "container-title": ["Journal of Examples"],
        "URL": "https://example.org/paper",
        "abstract": "Abstract text.",
        "type": "journal-article",
    }
    metadata = metadata_from_crossref(item)
    assert metadata["doi"] == "10.1234/Test"
    assert metadata["title"] == "A Paper"
    assert metadata["authors"] == ["Ada Lovelace", "Alan Turing", "OnlyGiven"]
    assert metadata["year"] == 2020
    assert metadata["venue"] == "Journal of Examples"
    assert metadata["url"] == "https://example.org/paper"
    assert metadata["abstract"] == "Abstract text."
    assert metadata["type"] == "journal-article"


def test_metadata_from_crossref_empty_item_defaults():
    metadata = metadata_from_crossref({})
    assert metadata["doi"] is None
    assert metadata["title"] is None
    assert metadata["authors"] == []
    assert metadata["year"] is None
    assert metadata["venue"] is None


def test_metadata_from_crossref_falls_back_to_issued_date():
    metadata = metadata_from_crossref({"issued": {"date-parts": [[1999]]}})
    assert metadata["year"] == 1999


def test_resolve_extracted_references_local_hit():
    citations, errors = resolve_extracted_references(
        [{"raw_reference": "Ref A", "doi": "10.1/a"}],
        lambda doi: ("paper-1", "notes/paper-1.md"),
        None,
    )
    assert errors == []
    assert citations[0]["status"] == "resolved"
    assert citations[0]["confidence"] == 1.0
    assert citations[0]["target_id"] == "paper-1"
    assert citations[0]["target_note"] == "notes/paper-1.md"


def test_resolve_extracted_references_external_hit():
    crossref = MagicMock()
    crossref.by_doi.return_value = {"DOI": "10.1/a"}
    citations, errors = resolve_extracted_references(
        [{"raw_reference": "Ref A", "doi": "10.1/a"}], lambda doi: None, crossref
    )
    assert errors == []
    assert citations[0]["status"] == "external"
    assert citations[0]["confidence"] == 0.95


def test_resolve_extracted_references_no_doi_is_unresolved():
    citations, errors = resolve_extracted_references(
        [{"raw_reference": "No DOI"}], lambda doi: None, None
    )
    assert errors == []
    assert citations[0]["status"] == "unresolved"
    assert citations[0]["confidence"] == 0.0


def test_resolve_extracted_references_propagates_grobid_metadata():
    grobid = {
        "title": "A cited paper",
        "authors": ["Ada Lovelace"],
        "year": 2023,
        "venue": "Journal of Examples",
    }
    citations, errors = resolve_extracted_references(
        [
            {
                "raw_reference": "Ada Lovelace. A cited paper. 2023.",
                "title": "A cited paper",
                "authors": ["Ada Lovelace"],
                "year": 2023,
                "venue": "Journal of Examples",
                "volume": "12",
                "issue": "3",
                "pages": "10-20",
                "publisher": "Example Press",
                "url": "https://example.org/cited",
                "grobid": grobid,
            }
        ],
        lambda doi: None,
        None,
    )

    assert errors == []
    citation = citations[0]
    assert citation["title"] == "A cited paper"
    assert citation["authors"] == ["Ada Lovelace"]
    assert citation["year"] == 2023
    assert citation["venue"] == "Journal of Examples"
    assert citation["volume"] == "12"
    assert citation["issue"] == "3"
    assert citation["pages"] == "10-20"
    assert citation["publisher"] == "Example Press"
    assert citation["url"] == "https://example.org/cited"
    assert citation["grobid"] == grobid


def test_resolve_extracted_references_records_http_errors():
    crossref = MagicMock()
    crossref.by_doi.side_effect = httpx.HTTPError("boom")
    citations, errors = resolve_extracted_references(
        [{"raw_reference": "Ref", "doi": "10.1/a"}], lambda doi: None, crossref
    )
    assert citations[0]["status"] == "unresolved"
    assert errors == ["10.1/a: boom"]
