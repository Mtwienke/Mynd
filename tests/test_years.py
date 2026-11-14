import json
from dataclasses import replace
from datetime import date
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mynd.clean import clean_vault
from mynd.cli import app
from mynd.resolution.publication_year import publication_year, valid_year
from mynd.settings import Settings
from mynd.vault.managed import block_range, put_block
from mynd.vault.markdown import parse_document
from mynd.vault.note_writer import PaperNote, write_paper_note, write_reference_placeholder
from mynd.vault.safety import Change, apply_changes, read_text, restore_run
from mynd.vault.year_notes import ensure_year_note, year_link, year_path
from mynd.years import organize_years


@pytest.fixture
def settings(tmp_path):
    result = Settings(tmp_path / "obsidian", tmp_path / "data", tmp_path / "inbox",
                      tmp_path / "pdfs", tmp_path / "library.sqlite3", use_grobid=False)
    result.vault_path.mkdir()
    return result


def write(settings, relative, content):
    path = settings.vault_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode())
    return path


def paper(settings, name="Paper.md", year=2023):
    return write(settings, name, f"---\ntype: paper\nyear: {year}\n---\n\n# Paper\n\n"
                 f"- **Year:** {year}\n\n## My notes\n\nPersonal content stays.  \nNext line.\n")


def old_placeholder(settings, year, raw, *, structured_year=None):
    dump = json.dumps({"year": structured_year, "raw_reference": raw}, indent=2)
    return write(settings, "Reference.md", f"---\ntype: paper-placeholder\nyear: {year}\n---\n\n"
                 f"# Reference\n\n- **Year:** [[{year}]]\n\n## GROBID Dump\n\n~~~json\n{dump}\n~~~\n\n"
                 "## My notes\n\nMy personal observations.\n")


@pytest.mark.parametrize(("year", "relative"), [
    (1995, "Years/90s/year-1995.md"), (2003, "Years/00s/year-2003.md"),
    (2016, "Years/10s/year-2016.md"), (2023, "Years/20s/year-2023.md"),
    (1958, "Years/50s/year-1958.md"), (1911, "Years/1910s/year-1911.md"),
    (1895, "Years/1890s/year-1895.md"),
])
def test_year_layout_is_century_safe(year, relative):
    assert year_path(year).as_posix() == relative
    assert year_link(year) == f"[[{relative.removesuffix('.md')}|{year}]]"


@pytest.mark.parametrize("value", [None, True, False, 999, 9999, "arxiv.2304.08503"])
def test_invalid_years_never_become_hubs(settings, value):
    assert valid_year(value) is None
    assert ensure_year_note(settings.vault_path, value) is None
    assert not (settings.vault_path / "Years").exists()


@pytest.mark.parametrize(("raw", "expected"), [
    ("N Hansen 10.48550/arXiv.1604.00772 arXiv. 2016", 2016),
    ("A study arXiv:2304.08503, 2023", 2023),
    ("Proc. Roy. Soc. A 478 no. 2260, 2022, Art. no. 20210904", 2022),
    ("Volume 2019, pp. 2020–2021 (2024)", 2024),
    ("Older work (1895)", 1895),
    ("Study 2019 and 2023", None),
    ("10.48550/arXiv.1604.00772", None),
    ("Study at https://example.org/2023/paper", None),
])
def test_publication_dates_do_not_come_from_identifiers(raw, expected):
    assert publication_year({}, raw) == expected


def test_explicit_publication_date_wins_over_fallback():
    assert publication_year({"year": 2021}, "Other dates 2019 and 2023") == 2021
    assert publication_year({"year": date.today().year + 20}, "Published 2022") == 2022


def test_ingestion_writes_resolvable_year_hubs_for_papers_and_references(settings):
    path = write_paper_note(PaperNote(settings.vault_path, "paper-abcdef123456",
                           {"title": "Paper", "year": 1995}, settings.pdf_store_path / "paper.pdf"))
    assert "- **Year:** [[Years/90s/year-1995|1995]]" in read_text(path)
    reference = write_reference_placeholder(settings.vault_path,
        {"raw_reference": "Study 10.48550/arXiv.2304.08503 arXiv. 2023", "title": "Study"}, "Paper", "one")
    assert "year: 2023" in read_text(reference)
    assert "[[Years/20s/year-2023|2023]]" in read_text(reference)
    for year in (1995, 2023):
        hub = settings.vault_path / year_path(year)
        doc = parse_document(hub, settings.vault_path)
        assert doc.kind == "year" and doc.metadata["year"] == year
        assert doc.metadata["tags"] == ["mynd/year"]


def test_ingestion_preserves_user_edits_to_year_hubs(settings):
    path = ensure_year_note(settings.vault_path, 2023)
    path.write_text(read_text(path) + "\n## My chronology\n\nImportant observations.\n")
    before, modified = read_text(path), path.stat().st_mtime_ns
    ensure_year_note(settings.vault_path, 2023)
    write_paper_note(PaperNote(settings.vault_path, "paper-abcdef123456", {"title": "Paper", "year": 2023},
                              settings.pdf_store_path / "paper.pdf"))
    assert read_text(path) == before and path.stat().st_mtime_ns == modified


def test_preview_then_migration_is_idempotent_and_resolves_cached_year_links(settings):
    source = paper(settings)
    reference = old_placeholder(settings, 2021, "Older work (2021)")
    before = {p: p.read_bytes() for p in (source, reference)}
    preview = organize_years(settings)
    assert len(preview["created"]) == 2 and len(preview["updated"]) == 2
    assert not settings.data_path.exists()
    assert before == {p: p.read_bytes() for p in before}
    clean_vault(settings)  # Old unresolved diagnostics must invalidate after migration.
    result = organize_years(settings, apply=True)
    assert result["status"] == "complete" and result["run_id"]
    assert "[[Years/20s/year-2023|2023]]" in read_text(source)
    assert "Personal content stays.  \nNext line." in read_text(source)
    findings = clean_vault(settings)
    assert not [f for n in findings["notes"] for f in n.get("findings", []) if f["status"] == "unresolved"]
    times = {p: p.stat().st_mtime_ns for p in settings.vault_path.rglob("*.md")}
    assert organize_years(settings, apply=True)["changed"] == 0
    assert times == {p: p.stat().st_mtime_ns for p in times}
    restore_run(settings, result["run_id"])
    assert before == {p: p.read_bytes() for p in before}
    assert not list((settings.vault_path / "Years").rglob("*.md"))


def test_moving_existing_year_preserves_content_and_updates_both_directions(settings):
    source = paper(settings)
    book = write(settings, "Books/Book.md", "# A book\n")
    original = ("# Year 2023\n\nPersonal year notes.\n\n"
                '[Book](../Books/Book.md "Hover title")\n\n'
                "## Events\n\nThings I recorded.\n")
    old = write(settings, "Calendar/2023.md", original)
    inbound = write(settings, "other.md", '[This year](Calendar/2023.md#Events "My label") '
                    "and [[Calendar/2023#Events|events]].\n")
    result = organize_years(settings, apply=True)
    target = settings.vault_path / year_path(2023)
    assert not old.exists() and target.exists() and book.exists()
    assert "Personal year notes." in read_text(target)
    assert '[Book](Books/Book.md "Hover title")' in read_text(target)
    assert "## Events\n\nThings I recorded." in read_text(target)
    assert '[This year](Years/20s/year-2023.md#Events "My label")' in read_text(inbound)
    assert "[[Years/20s/year-2023#Events|events]]" in read_text(inbound)
    assert "[[Years/20s/year-2023|2023]]" in read_text(source)
    restore_run(settings, result["run_id"])
    assert read_text(old) == original and not target.exists()


def test_duplicate_year_notes_are_never_merged_or_overwritten(settings):
    paper(settings)
    a = write(settings, "2023.md", "My first year page.")
    b = write(settings, "Other/year-2023.md", "My second, different year page.")
    with pytest.raises(ValueError, match="Conflicting year"):
        organize_years(settings, apply=True)
    assert read_text(a) == "My first year page." and read_text(b) == "My second, different year page."
    assert not (settings.vault_path / "Years").exists()


def test_identifier_year_bug_is_corrected_from_retained_source_not_guessed(settings):
    raw = "N Hansen 10.48550/arXiv.1604.00772 arXiv. 2016"
    path = old_placeholder(settings, 1604, raw)
    original = read_text(path)
    result = organize_years(settings, apply=True)
    assert result["corrected_years"][0]["old"] == 1604
    assert "year: 2016" in read_text(path)
    assert "[[Years/10s/year-2016|2016]]" in read_text(path)
    assert raw in read_text(path) and '"year": null' in read_text(path)
    assert "My personal observations." in read_text(path)
    assert not (settings.vault_path / "Years/1600s/year-1604.md").exists()
    restore_run(settings, result["run_id"])
    assert read_text(path) == original


def test_manual_year_override_and_unknown_year_are_not_inferred_away(settings):
    path = old_placeholder(settings, 2021, "Study 10.48550/arXiv.2304.08503 arXiv. 2023")
    unknown = write(settings, "unknown.md", "---\ntype: paper\nyear: null\n---\n\n# Unknown\n")
    result = organize_years(settings, apply=True)
    assert not result["corrected_years"]
    assert "year: 2021" in read_text(path)
    assert "[[Years/20s/year-2021|2021]]" in read_text(path)
    assert "- **Year:**" not in read_text(unknown)


def test_true_historical_year_gets_a_distinct_century_safe_folder(settings):
    path = paper(settings, year=1911)
    organize_years(settings, apply=True)
    assert (settings.vault_path / "Years/1910s/year-1911.md").exists()
    assert "[[Years/1910s/year-1911|1911]]" in read_text(path)


def test_links_in_code_stay_literal_while_table_aliases_are_escaped(settings):
    paper(settings)
    path = write(settings, "context.md", "# Context\n\n`[[2023]]` is an example.\n\n"
                 "| Year | Meaning |\n| --- | --- |\n| [[2023]] | Publication |\n")
    organize_years(settings, apply=True)
    assert "`[[2023]]` is an example." in read_text(path)
    assert "| [[Years/20s/year-2023\\|2023]] | Publication |" in read_text(path)


def test_generated_blocks_are_rechecksummed_without_changing_personal_text(settings):
    paper(settings)
    path = write(settings, "summary.md", put_block("# Summary\n\nPersonal prose stays.\n", "topic-content",
                 "## Generated\n\nSee [[2023]] for this research."))
    organize_years(settings, apply=True)
    assert "Personal prose stays." in read_text(path)
    assert "[[Years/20s/year-2023|2023]]" in block_range(read_text(path), "topic-content")[2]
    assert not parse_document(path, settings.vault_path).errors


def test_restore_checks_a_moved_year_hub_for_later_human_edits(settings):
    paper(settings)
    old = write(settings, "2023.md", "Important original chronology.")
    result = organize_years(settings, apply=True)
    target = settings.vault_path / year_path(2023)
    target.write_text(read_text(target) + "\nA new human edit.\n")
    with pytest.raises(ValueError, match="edited after"):
        restore_run(settings, result["run_id"])
    assert not old.exists() and "A new human edit." in read_text(target)


def test_years_command_defaults_to_preview_and_exposes_full_report(settings, config_file):
    paper(settings)
    runner = CliRunner()
    with patch("mynd.cli.load_settings", return_value=settings):
        preview = runner.invoke(app, ["years", "-c", str(config_file), "--json"])
        assert preview.exit_code == 0 and not json.loads(preview.stdout)["applied"]
        assert not (settings.vault_path / "Years").exists()
        applied = runner.invoke(app, ["years", "-c", str(config_file), "--apply", "--json"])
        assert applied.exit_code == 0 and json.loads(applied.stdout)["applied"]


def test_journal_can_restore_an_explicit_deletion(settings):
    path = write(settings, "old-year.md", "Original year notes.")
    result = apply_changes(settings, [Change(path, read_text(path), None)], "test-move")
    assert not path.exists()
    restore_run(settings, result)
    assert read_text(path) == "Original year notes."


def test_exclusions_protect_notes_and_year_destinations(settings):
    source = paper(settings)
    before = read_text(source)
    with pytest.raises(ValueError, match="excluded"):
        organize_years(replace(settings, maintenance_excludes=("Years/**",)), apply=True)
    assert read_text(source) == before


def test_quoted_yaml_links_are_updated_but_numeric_properties_and_comments_survive(settings):
    paper(settings)
    write(settings, "2023.md", "# 2023\n\nExisting context.\n")
    path = write(settings, "properties.md", '---\nyear: 2023 # Keep numeric type\n'
                 'related: ["[[2023]]"] # Keep this comment\n---\n\n# Personal note\n')
    organize_years(settings, apply=True)
    content = read_text(path)
    assert "year: 2023 # Keep numeric type" in content
    assert 'related: ["[[Years/20s/year-2023|2023]]"] # Keep this comment' in content


def test_canvas_file_references_follow_moved_year_notes(settings):
    paper(settings)
    write(settings, "2023.md", "# 2023\n\nExisting context.\n")
    original = '{"nodes": [{"id": "one", "type": "file", "file": "2023.md"}], "edges": []}\n'
    canvas = write(settings, "Chronology.canvas", original)
    result = organize_years(settings, apply=True)
    assert json.loads(read_text(canvas))["nodes"][0]["file"] == "Years/20s/year-2023.md"
    restore_run(settings, result["run_id"])
    assert read_text(canvas) == original
