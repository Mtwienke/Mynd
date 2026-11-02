"""Direct tests for the storage repository layer that owns all SQL."""

import sqlite3
from pathlib import Path

import pytest

from mynd.storage import repositories
from mynd.storage.database import initialize_database


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "library.sqlite3"
    initialize_database(path)
    return path


def _save(database: Path, paper_id: str = "doi-abc", **metadata) -> None:
    repositories.save_ingestion(
        database,
        paper_id,
        {"title": "A Paper", "authors": ["Ada"], **metadata},
        repositories.IngestedDocument(
            digest="digest-1",
            filename="a-paper.pdf",
            stored_path=Path("/library/a-paper.pdf"),
            text="full text",
        ),
    )


def test_save_ingestion_round_trips_metadata(database):
    _save(database, doi="10.1000/xyz", year=2024, venue="Journal")

    metadata = repositories.metadata_for_paper(database, "doi-abc")
    assert metadata is not None
    assert metadata["title"] == "A Paper"
    assert metadata["authors"] == ["Ada"]
    assert metadata["doi"] == "10.1000/xyz"
    assert metadata["year"] == 2024
    assert metadata["venue"] == "Journal"


def test_document_lookup_and_text(database):
    _save(database)

    found = repositories.document_for_digest(database, "digest-1")
    assert found is not None
    assert found.paper_id == "doi-abc"
    assert repositories.extracted_text_for_document(database, found.id) == "full text"


def test_paper_for_doi_round_trips(database):
    _save(database, doi="10.1000/xyz")
    repositories.set_paper_note_path(database, "doi-abc", Path("/vault/note.md"))

    found = repositories.paper_for_doi(database, "10.1000/xyz")
    assert found is not None
    assert found.id == "doi-abc"
    assert found.note_path == "/vault/note.md"


def test_resolved_citations_are_joined_with_target_note(database):
    _save(database, paper_id="source", doi="10.1000/source")
    _save(database, paper_id="target", doi="10.1000/target")
    repositories.set_paper_note_path(database, "target", Path("/vault/target.md"))

    repositories.insert_resolved_citations(
        database,
        "source",
        [
            {
                "raw_reference": "Target work",
                "doi": "10.1000/target",
                "target_id": "target",
                "target_note": "/vault/target.md",
                "status": "resolved",
            }
        ],
    )

    citations = repositories.citations_for_paper(database, "source")
    assert citations[0]["raw_reference"] == "Target work"
    assert citations[0]["target_note"] == "/vault/target.md"


def test_citations_round_trip_grobid_payload(database):
    _save(database, paper_id="source")
    grobid = {
        "title": "A cited paper",
        "authors": ["Ada Lovelace"],
        "year": 2023,
        "venue": "Journal of Examples",
        "raw_reference": "Target work",
        "tei": "<tei:biblStruct/>",
    }
    repositories.insert_resolved_citations(
        database,
        "source",
        [
            {
                "raw_reference": "Target work",
                "doi": "10.1000/target",
                "status": "unresolved",
                "grobid": grobid,
            }
        ],
    )

    citations = repositories.citations_for_paper(database, "source")
    assert citations[0]["grobid"] == grobid
    assert citations[0]["title"] == "A cited paper"
    assert citations[0]["authors"] == ["Ada Lovelace"]
    assert citations[0]["year"] == 2023


def test_insert_resolved_citations_refreshes_grobid_payload(database):
    _save(database, paper_id="source")
    citation = {"raw_reference": "Target work", "status": "unresolved"}
    repositories.insert_resolved_citations(
        database, "source", [{**citation, "grobid": {"title": "Old title"}}]
    )
    repositories.insert_resolved_citations(
        database, "source", [{**citation, "grobid": {"title": "New title"}}]
    )

    citations = repositories.citations_for_paper(database, "source")
    assert len(citations) == 1
    assert citations[0]["grobid"]["title"] == "New title"


def test_unresolved_citations_include_source_title(database):
    _save(database, paper_id="source", doi="10.1000/source", title="Source Paper")
    repositories.insert_resolved_citations(
        database,
        "source",
        [{"raw_reference": "Missing", "doi": "10.9/missing", "status": "unresolved"}],
    )

    pending = repositories.unresolved_citations(database)
    assert pending == [
        repositories.PendingCitation(
            title="Source Paper",
            raw_reference="Missing",
            doi="10.9/missing",
            status="unresolved",
        )
    ]


def test_foreign_keys_are_enforced_on_write_connections(database):
    with pytest.raises(sqlite3.IntegrityError):
        repositories.insert_resolved_citations(
            database,
            "missing-paper",
            [{"raw_reference": "Orphan", "status": "unresolved"}],
        )
