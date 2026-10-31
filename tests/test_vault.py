from pathlib import Path

from mynd.files import atomic_write_text
from mynd.identity import migrate_legacy_note, slugify
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
        PaperNote(tmp_path / "vault", "paper-abcdef123456", metadata, pdf, summary="My summary")
    )
    content = path.read_text(encoding="utf-8")
    assert "# A Study" in content
    assert "Ada Lovelace" in content
    assert "My summary" in content
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


def test_write_reference_placeholder_is_idempotent(tmp_path):
    vault = tmp_path / "vault"
    citation = {"raw_reference": "Ref", "doi": "10.1234/ref"}
    first = write_reference_placeholder(vault, citation)
    second = write_reference_placeholder(vault, citation)
    assert first == second
    assert "reference-placeholder" in first.read_text(encoding="utf-8")


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
