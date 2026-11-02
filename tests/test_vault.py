from pathlib import Path

from mynd.files import atomic_write_text
from mynd.identity import migrate_legacy_note, note_filename, slugify
from mynd.vault.note_writer import (
    PaperNote,
    _yaml_quote,
    link_summary_citations,
    write_paper_note,
    write_reference_placeholder,
)


def test_slug_strips_unsafe_characters():
    assert slugify("A/B: C?") == "AB C"
    assert slugify("   ") == "Untitled Paper"


def test_yaml_quote_escapes_values():
    assert _yaml_quote('he said "hi"') == '"he said \\"hi\\""'
    assert _yaml_quote("line\nbreak") == '"line break"'


def test_atomic_write_text_creates_parents(tmp_path):
    target = tmp_path / "nested" / "note.md"
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert not target.with_suffix(".md.tmp").exists()


def test_write_paper_note_creates_note_with_sections(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    metadata = {
        "title": "A Study",
        "authors": ["Ada Lovelace"],
        "doi": "10.1234/study",
        "year": 2021,
        "venue": "Journal",
        "url": "https://example.org",
        "abstract": "An abstract.",
    }
    path = write_paper_note(
        PaperNote(
            tmp_path / "vault",
            "paper-abcdef123456",
            metadata,
            pdf,
            summary="My summary",
            questions="### Main idea\n\n1. What is it?",
        )
    )
    content = path.read_text(encoding="utf-8")
    assert "# A Study" in content
    assert "Ada Lovelace" in content
    assert "My summary" in content
    assert "## Extracted questions" in content
    assert "### Main idea" in content
    assert "## Provenance" in content


def test_write_paper_note_preserves_human_sections(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    metadata = {"title": "A Study", "authors": [], "doi": None, "year": None}
    vault = tmp_path / "vault"
    path = write_paper_note(PaperNote(vault, "paper-abcdef123456", metadata, pdf, summary="First"))

    content = path.read_text(encoding="utf-8")
    content = content.replace(
        "_Add your own interpretation here. This section is preserved during note updates._",
        "My private notes",
    )
    path.write_text(content, encoding="utf-8")

    write_paper_note(PaperNote(vault, "paper-abcdef123456", metadata, pdf, summary="Second"))
    updated = path.read_text(encoding="utf-8")
    assert "My private notes" in updated
    assert "Second" in updated


def test_write_paper_note_keeps_existing_summary_without_new_one(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    metadata = {"title": "A Study", "authors": [], "doi": None, "year": None}
    vault = tmp_path / "vault"
    path = write_paper_note(PaperNote(vault, "paper-abcdef123456", metadata, pdf, summary="First"))

    write_paper_note(PaperNote(vault, "paper-abcdef123456", metadata, pdf, summary=None))
    updated = path.read_text(encoding="utf-8")
    assert "First" in updated
    assert "_Summary unavailable" not in updated


def test_write_paper_note_preserves_existing_extracted_questions(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    metadata = {"title": "A Study", "authors": [], "doi": None, "year": None}
    vault = tmp_path / "vault"
    path = write_paper_note(
        PaperNote(
            vault,
            "paper-abcdef123456",
            metadata,
            pdf,
            summary="First",
            questions="### Main idea\n\n1. Original question?",
        )
    )

    write_paper_note(PaperNote(vault, "paper-abcdef123456", metadata, pdf, summary="Second"))
    updated = path.read_text(encoding="utf-8")
    assert "Original question?" in updated
    assert updated.count("## Extracted questions") == 1


def _citation_with_grobid() -> dict:
    grobid = {
        "title": "A cited paper",
        "authors": ["Ada Lovelace", "Alan Turing"],
        "year": 2023,
        "venue": "Journal of Examples",
        "volume": "12",
        "issue": "3",
        "pages": "10-20",
        "publisher": "Example Press",
        "url": "https://example.org/cited",
        "doi": "10.1234/ref",
        "identifiers": {"doi": "10.1234/ref", "arxiv": "2401.00001"},
        "raw_reference": "Ada Lovelace. A cited paper. Journal of Examples, 12(3):10-20, 2023.",
        "tei": "<tei:biblStruct><analytic><title>A cited paper</title></analytic></tei:biblStruct>",
    }
    return {
        "raw_reference": grobid["raw_reference"],
        "doi": grobid["doi"],
        "title": grobid["title"],
        "authors": grobid["authors"],
        "year": grobid["year"],
        "venue": grobid["venue"],
        "volume": grobid["volume"],
        "issue": grobid["issue"],
        "pages": grobid["pages"],
        "publisher": grobid["publisher"],
        "url": grobid["url"],
        "grobid": grobid,
        "status": "unresolved",
    }


def test_write_reference_placeholder_is_idempotent(tmp_path):
    vault = tmp_path / "vault"
    citation = {"raw_reference": "Ref", "doi": "10.1234/ref"}
    first = write_reference_placeholder(vault, citation, "Citing Paper", "citing-id")
    second = write_reference_placeholder(vault, citation, "Citing Paper", "citing-id")
    assert first == second
    assert "reference-placeholder" in first.read_text(encoding="utf-8")


def test_write_reference_placeholder_saves_grobid_fields_and_dump(tmp_path):
    vault = tmp_path / "vault"
    path = write_reference_placeholder(
        vault, _citation_with_grobid(), "Citing Paper", "citing-id"
    )

    content = path.read_text(encoding="utf-8")
    assert "## GROBID Dump" in content
    assert "year: 2023" in content
    assert "[[2023]]" in content
    assert '"arxiv": "2401.00001"' in content
    assert "<tei:biblStruct>" in content
    assert "- **Venue:** Journal of Examples" in content
    assert "- **Volume:** 12" in content
    assert "- **Issue:** 3" in content
    assert "- **Pages:** 10-20" in content
    assert "- **Publisher:** Example Press" in content
    assert "- **URL:** https://example.org/cited" in content
    assert '"status": "unresolved"' in content


def test_write_reference_placeholder_accumulates_citations_and_keeps_notes(tmp_path):
    vault = tmp_path / "vault"
    citation = _citation_with_grobid()
    path = write_reference_placeholder(vault, citation, "First Paper", "first-id")

    content = path.read_text(encoding="utf-8").replace(
        "_Add your own notes about this reference here._", "My private note."
    )
    path.write_text(content, encoding="utf-8")

    write_reference_placeholder(vault, citation, "Second Paper", "second-id")
    updated = path.read_text(encoding="utf-8")

    assert "[[First Paper (first-id)]]" in updated
    assert "[[Second Paper (second-id)]]" in updated
    assert updated.count("[[First Paper (first-id)]]") == 2  # frontmatter and body
    assert "My private note." in updated


def test_write_reference_placeholder_falls_back_to_raw_year(tmp_path):
    vault = tmp_path / "vault"
    citation = {"raw_reference": "An older work (2021)", "doi": "10.1234/old"}
    path = write_reference_placeholder(vault, citation, "Citing Paper", "citing-id")

    content = path.read_text(encoding="utf-8")
    assert "year: 2021" in content
    assert "[[2021]]" in content


def test_placeholder_cited_by_points_to_actual_citing_note(tmp_path):
    vault = tmp_path / "vault"
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    title = "A/B: C? | D"
    paper_id = "doi-abcdef1234567890"
    citing_note = write_paper_note(
        PaperNote(vault, paper_id, {"title": title, "authors": []}, pdf)
    )

    placeholder = write_reference_placeholder(
        vault, {"raw_reference": "Ref", "doi": "10.1234/ref"}, title, paper_id
    )
    content = placeholder.read_text(encoding="utf-8")

    assert f"[[{citing_note.stem}]]" in content
    assert "A/B" not in content
    assert f"({paper_id})" not in content


def test_placeholder_repairs_legacy_cited_by_link(tmp_path):
    vault = tmp_path / "vault"
    title = "A/B: C? | D"
    paper_id = "doi-abcdef1234567890"
    citation = {"raw_reference": "Ref", "doi": "10.1234/ref"}
    path = write_reference_placeholder(vault, citation, title, paper_id)
    canonical = f"[[{Path(note_filename(title, paper_id)).stem}]]"
    legacy = f"[[{title} ({paper_id})]]"
    path.write_text(
        path.read_text(encoding="utf-8").replace(canonical, legacy), encoding="utf-8"
    )

    write_reference_placeholder(vault, citation, title, paper_id)
    updated = path.read_text(encoding="utf-8")

    assert canonical in updated
    assert legacy not in updated


def test_link_summary_citations_replaces_numbers(tmp_path):
    vault = tmp_path / "vault"
    note = vault / "10 Papers" / "Target (abc123).md"
    atomic_write_text(note, "note")
    summary = "See [1] and [2, 3]."
    citations = [
        {"raw_reference": "one", "target_note": note},
        {"raw_reference": "two", "placeholder_note": note},
        {"raw_reference": "three"},
    ]
    linked = link_summary_citations(vault, summary, citations)
    assert "[[10 Papers/Target (abc123)]]" in linked
    assert ", 3)" in linked


def test_link_summary_citations_none_summary():
    assert link_summary_citations(Path("/tmp"), None, []) is None


def test_migrate_legacy_note_renames(tmp_path):
    legacy = tmp_path / "Title [abcdef123456].md"
    legacy.write_text("x", encoding="utf-8")
    migrated = migrate_legacy_note(legacy)
    assert migrated.name == "Title (abcdef123456).md"
    assert migrated.exists()
    assert not legacy.exists()


def test_untrusted_titles_cannot_inject_note_sections(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    metadata = {"title": "Innocent\n## Summary\nHijacked", "authors": []}

    note = write_paper_note(PaperNote(tmp_path / "vault", "paper-abc123def456", metadata, pdf))

    content = note.read_text(encoding="utf-8")
    summary_headings = [line for line in content.splitlines() if line.startswith("## Summary")]
    assert summary_headings == ["## Summary"]
    assert "Hijacked" in content
