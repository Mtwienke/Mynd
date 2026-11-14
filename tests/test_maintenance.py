import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from mynd.ai.topics import InvalidModelOutput
from mynd.clean import clean_vault
from mynd.cli import app
from mynd.connect import Decision, connect_vault
from mynd.settings import Settings
from mynd.storage.maintenance import MaintenanceStore
from mynd.vault.connections import connect_note, preserve_connections
from mynd.vault.course_notes import _write as write_course_blocks
from mynd.vault.managed import block_range, owned_links, put_block, unwrap_links
from mynd.vault.markdown import LinkIndex, parse_document, passages, vault_files
from mynd.vault.note_writer import PaperNote, write_paper_note
from mynd.vault.safety import Change, apply_changes, read_text, restore_run


@pytest.fixture
def settings(tmp_path):
    result = Settings(tmp_path / "obsidian", tmp_path / "data", tmp_path / "inbox",
                      tmp_path / "pdfs", tmp_path / "library.sqlite3", use_grobid=False)
    result.vault_path.mkdir()
    return result


def note(settings, name, text, kind="note", work_id=""):
    path = settings.vault_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"---\ntype: {kind}\n" + (f"work_id: {work_id}\n" if work_id else "") + "---\n\n"
    path.write_text(prefix + text)
    return path


class FakeWriter:
    """Coordinator tests use no model process, downloads, or outbound requests."""
    version = "fake-topics-v1"

    def __init__(self):
        self.generator = SimpleNamespace(model="test-local")
        self.calls = []
        self.fail = False
        # These orchestration fixtures model a broad concept. Real breadth and
        # exclusion behavior is exercised separately in test_connect_policy.py.
        self.breadth = "umbrella"

    def called(self, name):
        assert not self.fail, f"Unexpected model call: {name}"
        self.calls.append(name)

    def extract(self, sources):
        self.called("extract")
        result = []
        for source in sources:
            if "self-attention" in source.text.lower():
                result.append({"id": "candidate-" + source.id, "title": "Self-attention",
                               "definition": "A mechanism for weighting tokens in a sequence.",
                               "aliases": ["self-attention"], "sources": [source.id],
                               "role": "mention" if "passing" in source.text else "central",
                               "important": "key concept" in source.text,
                               "rationale": "This is a core mechanism worth understanding."})
        return result

    def consolidate(self, candidates, known):
        self.called("consolidate")
        return [{"title": "Self-attention", "definition": candidates[0]["definition"],
                 "aliases": ["self-attention"], "members": [c["id"] for c in candidates],
                 "topic_id": known[0]["id"] if known else ""}]

    def match(self, topic, sources):
        self.called("match")
        return [{"source": p.id, "term": "self-attention", "supports": "passing" not in p.text}
                for p in sources if "self-attention" in p.text.lower()]

    def classify(self, topics):
        self.called("classify")
        return {topic["id"]: {"breadth": self.breadth, "kind": "concept",
                              "reason": "A broad subject in this coordinator fixture.", "broader": []}
                for topic in topics}

    def summarize(self, material, title, *, web=False):
        if not material:
            return []
        self.called("web-summary" if web else "summary")
        identifiers = list(dict.fromkeys(item["id"] for item in material))
        return [{"text": "Self-attention weights relevant token information.", "sources": identifiers}]


def approve(proposal, known):
    return Decision("approve")


def test_parser_protects_obsidian_code_math_comments_and_existing_links(settings):
    content = """# Self-attention

Self-attention connects tokens. `[[Not a link]]` and $x = [[ignored]]$.

```md
[[Example]]
# Fake heading
```

%% [[Hidden]] %%

[[Target#Heading|Alias]] and [display](Target.md#Heading).

> Self-attention is quoted.
"""
    path = note(settings, "source.md", content)
    doc = parse_document(path, settings.vault_path)
    assert not doc.errors
    assert [h.text for h in doc.headings] == ["Self-attention"]
    assert [link.target for link in doc.links] == ["Target", "Target.md"]
    assert "Self-attention connects" in doc.inline_mask
    assert "Self-attention is quoted" not in doc.inline_mask
    assert "Not a link" not in doc.inline_mask


def test_parser_reports_malformed_yaml_wikilinks_and_fences(settings):
    for name, text, rule in [
        ("yaml.md", "---\ntype: paper\ntype: lecture\n---\n", "frontmatter-invalid"),
        ("wiki.md", "# Note\n\n[[Broken\n", "wikilink-malformed"),
        ("code.md", "# Note\n\n```python\nx = 1\n", "fence-unclosed"),
    ]:
        path = settings.vault_path / name
        path.write_text(text)
        doc = parse_document(path, settings.vault_path)
        assert rule in {item["rule"] for item in doc.errors}


def test_passages_ignore_templates_bibliographies_questions_and_topic_blocks(settings):
    text = """# Paper

- **Authors:** Someone

## Summary

Self-attention weights tokens in a meaningful sequence.

## Extracted questions

Self-attention question which must not count as additional evidence.

## My notes

My own substantive interpretation of self-attention belongs here.

## References

- [[A title mentioning self-attention]]
"""
    path = note(settings, "paper.md", text, "paper")
    doc = parse_document(path, settings.vault_path)
    first = passages(doc, "paper-one")
    assert len(first) == 2
    assert "My own" in first[1].text
    assert "question" not in " ".join(p.text for p in first)
    overlay = connect_note(doc.content, "paper-one", [{
        "topic_id": "topic-abc", "target": "20 Topics/Attention", "title": "Self-attention",
        "mentions": [{"source": first[0].id, "term": "Self-attention"}],
    }])
    second = passages(parse_document(path, settings.vault_path, overlay), "paper-one")
    assert [(p.id, p.text) for p in first] == [(p.id, p.text) for p in second]


def test_inline_overlay_is_first_mention_only_and_idempotent(settings):
    path = note(settings, "note.md", "# Self-attention\n\nSelf-attention uses tokens. "
                "Self-attention is repeated.\n\n`Self-attention` $Self-attention$\n")
    before = read_text(path)
    source = passages(parse_document(path, settings.vault_path), "one")[0]
    assignments = [{"topic_id": "topic-one", "target": "20 Topics/Self-attention", "title": "Self-attention",
                    "mentions": [{"source": source.id, "term": "Self-attention"}]}]
    first = connect_note(before, "one", assignments)
    assert len(owned_links(first)) == 1
    assert "# Self-attention\n" in first
    assert "`Self-attention` $Self-attention$" in first
    assert connect_note(first, "one", assignments) == first
    assert unwrap_links(first).startswith(before)


def test_ownership_edits_are_never_overwritten():
    original = put_block("# Note\n", "topics", "## Related topics\n\n- [[A]]")
    edited = original.replace("[[A]]", "[[B]]")
    with pytest.raises(ValueError, match="edited"):
        put_block(edited, "topics", "replacement")


def test_vault_scan_excludes_hidden_symlinks_and_configured_paths(settings, tmp_path):
    visible = note(settings, "public.md", "A visible note with meaningful prose.")
    note(settings, ".trash/old.md", "Deleted note.")
    note(settings, "Private/secret.md", "Private note.")
    external = tmp_path / "external.md"
    external.write_text("external")
    (settings.vault_path / "linked.md").symlink_to(external)
    assert vault_files(replace(settings, maintenance_excludes=("Private/**",))) == [visible]


def test_link_resolution_reports_ambiguous_basename(settings):
    a = note(settings, "A/Name.md", "Some substantive text in A.")
    b = note(settings, "B/Name.md", "Some substantive text in B.")
    path = note(settings, "Source.md", "See [[Name]] for details.")
    doc = parse_document(path, settings.vault_path)
    assert set(LinkIndex(settings.vault_path, [a, b, path]).resolve(doc, doc.links[0])) == {a, b}


def test_clean_is_report_only_then_repairs_known_heading_and_jats(settings):
    path = note(settings, "paper.md", "# Paper\n\n## Summary\n\n\\## Overview  \n"
                "Some prose with two spaces.  \nNext line.\n\n## Abstract\n\n"
                "<jats:p>An imported <jats:italic>abstract</jats:italic>.</jats:p>\n\n"
                "## My notes\n\nLiteral `\\## Overview` must remain.\n", "paper")
    before = read_text(path)
    report = clean_vault(settings)
    assert read_text(path) == before
    assert next(n for n in report["notes"] if n["path"] == "paper.md")["repairs"] == 2
    applied = clean_vault(settings, apply=True)
    after = read_text(path)
    assert "### Overview" in after
    assert "Some prose with two spaces.  \nNext line." in after
    assert "An imported abstract." in after
    assert "Literal `\\## Overview`" in after
    assert applied["run_id"]
    restore_run(settings, applied["run_id"])
    assert read_text(path) == before


def test_clean_repairs_identity_backed_paths_not_unresolved_years(settings):
    target = note(settings, "References/New title (abcdef123456).md", "A cited paper.",
                  "paper-placeholder", "doi-abcdef123456")
    path = note(settings, "source.md", "See [[References/Old title (abcdef123456)|Original wording]] "
                "and [[2023]], plus [[references/new title (abcdef123456)]].")
    result = clean_vault(settings, apply=True)
    text = read_text(path)
    assert "[[References/New title (abcdef123456)|Original wording]]" in text
    assert "[[2023]]" in text
    assert target.exists()
    assert result["changed"] == 1
    assert any(f["status"] == "unresolved" for n in result["notes"] for f in n["findings"])


def test_clean_rechecks_inbound_anchors_when_target_heading_changes(settings):
    target = note(settings, "target.md", "# Original\n\nSome substantive target prose.")
    note(settings, "source.md", "See [[target#Original]].")
    clean_vault(settings)
    cached = clean_vault(settings)
    assert cached["checked"] == 0
    target.write_text(read_text(target).replace("# Original", "# Renamed"))
    updated = clean_vault(settings)
    assert any(f["rule"] == "unresolved-anchor" for n in updated["notes"] for f in n["findings"])


def test_clean_preserves_crlf_and_refuses_damaged_markers(settings):
    path = settings.vault_path / "windows.md"
    path.write_bytes(b"# Heading\r\n\r\nText with hard break.  \r\n")
    original = path.read_bytes()
    bad = note(settings, "bad.md", "# Note\n\n<!-- mynd:summary:begin -->\nmissing end\n")
    report = clean_vault(settings, apply=True)
    assert path.read_bytes() == original
    assert "missing end" in read_text(bad)
    assert any(f["rule"] == "managed-block-damaged" for n in report["notes"] for f in n["findings"])


def test_journal_checks_concurrency_and_recovery_never_overwrites_new_work(settings):
    path = note(settings, "note.md", "Before.")
    original = read_text(path)
    with pytest.raises(ValueError, match="Concurrent"):
        apply_changes(settings, [Change(path, "wrong preimage", "After")], "test")
    run = apply_changes(settings, [Change(path, original, "After")], "test")
    path.write_text("Human edit after the run")
    with pytest.raises(ValueError, match="edited after"):
        restore_run(settings, run)
    assert read_text(path) == "Human edit after the run"


def test_journal_can_restore_a_partially_written_run(settings):
    first = note(settings, "one.md", "One original.")
    second = note(settings, "two.md", "Two original.")
    before = [read_text(first), read_text(second)]
    from mynd.vault import safety
    real_write = safety.atomic_write_text

    def interrupt(path, text):
        if path == second:
            raise OSError("simulated interruption")
        real_write(path, text)

    with patch("mynd.vault.safety.atomic_write_text", side_effect=interrupt), pytest.raises(OSError):
        apply_changes(settings, [Change(first, before[0], "one changed"),
                                 Change(second, before[1], "two changed")], "test")
    manifest = next(settings.data_path.rglob("manifest.json"))
    restore_run(settings, manifest.parent.name)
    assert [read_text(first), read_text(second)] == before


def test_connect_requires_review_then_second_run_has_no_model_calls_or_writes(settings):
    a = note(settings, "one.md", "# One\n\nSelf-attention weights tokens based on their relevance.")
    b = note(settings, "two.md", "# Two\n\nSelf-attention provides a contextual token representation.")
    writer = FakeWriter()
    pending = connect_vault(settings, writer)
    assert pending["status"] == "pending_review"
    assert pending["changed"] == 0
    result = connect_vault(settings, writer, reviewer=approve)
    assert result["status"] == "complete"
    assert result["changed"] == 3
    assert len(owned_links(read_text(a))) == len(owned_links(read_text(b))) == 1
    topic_path = settings.vault_path / result["topics"][0]["path"]
    text = read_text(topic_path)
    assert "AI-generated synthesis" in text and "[[one#One|one]]" in text
    assert "[[two#Two|two]]" in text
    assert "Web overview — AI generated" in text
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (a, b, topic_path)}
    writer.fail = True
    repeated = connect_vault(settings, writer)
    assert repeated["changed"] == 0
    assert repeated["cached"] == 2
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (a, b, topic_path)}


def test_new_topic_backfills_a_passing_mention_in_an_unchanged_note(settings):
    old = note(settings, "old.md", "# Old\n\nA passing mention of self-attention exists in this note.")
    writer = FakeWriter()
    assert not connect_vault(settings, writer)["pending"]
    note(settings, "new.md", "# New\n\nSelf-attention is a key concept for contextual token representations.")
    result = connect_vault(settings, writer, reviewer=approve)
    assert result["status"] == "complete"
    assert len(owned_links(read_text(old))) == 1
    assert result["topics"][0]["supporting"] == 1


def test_placeholders_and_course_overviews_never_create_shared_support(settings):
    note(settings, "one.md", "# One\n\nSelf-attention is discussed in this substantive note.")
    note(settings, "placeholder.md", "# Self-attention\n\nA bibliography-only self-attention title.",
         "paper-placeholder")
    note(settings, "overview.md", "# Course\n\nSelf-attention is repeated in this derived overview.", "course")
    note(settings, "year.md", "# Year\n\nSelf-attention is a key concept mentioned in this year hub.", "year")
    note(settings, "decade.md", "# Decade\n\nSelf-attention is a key concept mentioned in this decade hub.", "decade")
    note(settings, "Years/20s/year-2023.md", "# Year\n\nSelf-attention is a key concept mentioned here.")
    writer = FakeWriter()
    writer.breadth = "established"
    result = connect_vault(settings, writer)
    assert not result["pending"] and not result["topics"]


def test_rejection_is_remembered_and_explicit_review_can_reopen_it(settings):
    note(settings, "one.md", "# One\n\nSelf-attention is a key concept for understanding tokens.")
    writer = FakeWriter()
    connect_vault(settings, writer, reviewer=lambda *_: Decision("reject"))
    def unexpected(*_):
        pytest.fail("Rejected topic must not be proposed again unchanged")
    writer.fail = True
    assert not connect_vault(settings, writer, reviewer=unexpected)["pending"]
    writer.fail = False
    assert connect_vault(settings, writer, reviewer=approve, reopen=True)["topics"]


def test_connect_dry_run_does_not_create_state_or_vault_files(settings):
    path = note(settings, "one.md", "# One\n\nSelf-attention is a key concept for understanding tokens.")
    original = read_text(path)
    result = connect_vault(settings, FakeWriter(), reviewer=approve, dry_run=True)
    assert result["changed"] == 2 and result["run_id"] is None
    assert read_text(path) == original
    assert not settings.data_path.exists()
    assert not settings.database_path.exists()
    assert list(settings.vault_path.iterdir()) == [path]


def test_connect_updates_after_deletion_without_deleting_topic_or_personal_notes(settings):
    a = note(settings, "one.md", "# One\n\nSelf-attention weights tokens based on their relevance.")
    b = note(settings, "two.md", "# Two\n\nSelf-attention provides contextual token representations.")
    writer = FakeWriter()
    first = connect_vault(settings, writer, reviewer=approve)
    topic = settings.vault_path / first["topics"][0]["path"]
    topic.write_text(read_text(topic).replace("_Add personal notes here; this area is never regenerated._",
                                              "My personal interpretation is preserved."))
    a.unlink()
    b.write_text("# Changed\n\nA completely different topic now occupies this note.\n")
    result = connect_vault(settings, writer)
    assert result["status"] == "complete"
    assert topic.exists()
    assert "No substantive supporting passages remain" in read_text(topic)
    assert "My personal interpretation is preserved." in read_text(topic)
    assert not owned_links(read_text(b))


def test_manual_note_rename_reuses_identity_and_refreshes_citations_without_inference(settings):
    path = note(settings, "one.md", "# One\n\nSelf-attention is a key concept for understanding tokens.")
    writer = FakeWriter()
    first = connect_vault(settings, writer, reviewer=approve)
    moved = path.with_name("renamed.md")
    path.rename(moved)
    writer.fail = True
    result = connect_vault(settings, writer)
    assert result["cached"] == 1 and result["status"] == "complete"
    topic = settings.vault_path / first["topics"][0]["path"]
    assert "[[renamed#One|renamed]]" in read_text(topic)


def test_edited_topic_block_is_reported_without_overwriting_it(settings):
    source = note(settings, "one.md", "# One\n\nSelf-attention is a key concept for understanding tokens.")
    writer = FakeWriter()
    first = connect_vault(settings, writer, reviewer=approve)
    topic = settings.vault_path / first["topics"][0]["path"]
    topic.write_text(read_text(topic).replace("weights relevant", "my custom words for"))
    before = read_text(topic)
    result = connect_vault(settings, writer)
    assert result["status"] == "partial"
    assert read_text(topic) == before
    assert owned_links(read_text(source))


def test_existing_manual_topic_requires_explicit_adoption_and_stays_intact(settings):
    source = note(settings, "paper.md", "# Paper\n\nSelf-attention is a key concept for understanding tokens.")
    manual = note(settings, "Self-attention.md", "# My existing concept\n\nHuman-authored definition stays.")
    before = read_text(manual)
    def adopt(proposal, _):
        assert "Self-attention.md" in proposal.possible_matches
        return Decision("approve", adopt_path="Self-attention.md")
    result = connect_vault(settings, FakeWriter(), reviewer=adopt)
    assert result["status"] == "complete"
    assert read_text(manual).startswith(before)
    assert len(owned_links(read_text(source))) == 1
    assert result["topics"][0]["path"] == "Self-attention.md"


def test_paper_regeneration_preserves_connectors_custom_sections_and_frontmatter(settings):
    pdf = settings.pdf_store_path / "paper.pdf"
    metadata = {"title": "Paper", "authors": ["Ada"], "abstract": "An abstract."}
    paper = PaperNote(settings.vault_path, "paper-abcdef123456", metadata, pdf,
                      summary="## Overview\n\nSelf-attention is a key concept for understanding tokens.")
    path = write_paper_note(paper)
    path.write_text(read_text(path).replace("status: inbox", "status: reading\ncustom: keep-me")
                    + "\n## Custom chapter\n\nMy exact private prose.  \nSecond line.\n")
    writer = FakeWriter()
    connect_vault(settings, writer, reviewer=approve)
    before = read_text(path)
    updated = write_paper_note(replace(paper, summary=None))
    after = read_text(updated)
    assert "### Overview" in after and "\\## Overview" not in after
    assert "custom: keep-me" in after and "status: reading" in after
    assert "## Custom chapter\n\nMy exact private prose.  \nSecond line.\n" in after
    assert len(owned_links(after)) == 1
    assert block_range(before, "topics")[2] == block_range(after, "topics")[2]
    assert "&lt;!-- mynd:link" not in after


def test_preservation_does_not_reapply_inline_links_to_changed_passages(settings):
    path = note(settings, "source.md", "# Source\n\nSelf-attention weights tokens based on relevance.")
    before = read_text(path)
    source = passages(parse_document(path, settings.vault_path), "overlay")[0]
    connected = connect_note(before, "overlay", [{"topic_id": "topic-one", "title": "Self-attention",
        "target": "20 Topics/Self-attention", "mentions": [{"source": source.id, "term": "Self-attention"}]}])
    rendered = before.replace("weights tokens based on relevance", "now appears in a different context")
    preserved = preserve_connections(connected, rendered)
    assert not owned_links(preserved)
    assert "Connections may be stale" in preserved


def test_commands_expose_help_and_clean_json_without_model_calls(settings, config_file):
    runner = CliRunner()
    for command in ("clean", "connect"):
        assert runner.invoke(app, [command, "--help"]).exit_code == 0
    note(settings, "note.md", "# Note\n\nNormal readable content.")
    with patch("mynd.cli.load_settings", return_value=settings), patch("mynd.cli.build_text_generator") as model:
        result = runner.invoke(app, ["clean", "--config", str(config_file), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["changed"] == 0
    model.assert_not_called()


def test_store_is_vault_scoped_and_dry_reads_do_not_initialize_database(settings):
    store = MaintenanceStore(settings)
    assert not settings.database_path.exists()
    store.put("test", "key", {"value": "one"})
    store.save()
    other = MaintenanceStore(replace(settings, vault_path=settings.vault_path / "other"))
    assert other.get("test", "key") is None
    assert MaintenanceStore(settings).get("test", "key") == {"value": "one"}


class FakeWeb:
    def __init__(self):
        self.calls = []
        self.fail = False

    def research(self, query):
        self.calls.append(query)
        if self.fail:
            raise ValueError("Search rate limited")
        return {"query": query, "fetched_at": datetime.now(UTC).isoformat(), "sources": [
            {"id": "web-one", "url": "https://example.org/attention", "title": "Attention reference",
             "text": "Self-attention weights relevant token information. " * 8},
        ]}


def test_web_enrichment_is_cached_and_offline_retains_existing_overview(settings):
    note(settings, "one.md", "# One\n\nSelf-attention is a key concept for understanding tokens.")
    writer, web = FakeWriter(), FakeWeb()
    first = connect_vault(settings, writer, reviewer=approve, web_enabled=True, web_client=web)
    assert first["status"] == "complete"
    topic = settings.vault_path / first["topics"][0]["path"]
    assert "[Attention reference](<https://example.org/attention>)" in read_text(topic)
    assert len(web.calls) == 1 and "web-summary" in writer.calls
    writer.fail = True
    assert connect_vault(settings, writer, web_enabled=True, web_client=web)["changed"] == 0
    assert connect_vault(settings, writer, web_enabled=False, web_client=web)["changed"] == 0
    assert len(web.calls) == 1


def test_web_failure_keeps_valid_content_and_retries_only_failed_stage(settings):
    note(settings, "one.md", "# One\n\nSelf-attention is a key concept for understanding tokens.")
    writer, web = FakeWriter(), FakeWeb()
    web.fail = True
    first = connect_vault(settings, writer, reviewer=approve, web_enabled=True, web_client=web)
    assert first["status"] == "partial" and first["changed"] == 2
    assert first["errors"][0]["stage"] == "web"
    topic = settings.vault_path / first["topics"][0]["path"]
    assert "rate limited" in read_text(topic)
    assert "AI-generated synthesis" in read_text(topic)
    prior = writer.calls.copy()
    web.fail = False
    second = connect_vault(settings, writer, web_enabled=True, web_client=web)
    assert second["status"] == "complete"
    assert writer.calls == [*prior, "web-summary"]
    assert "rate limited" not in read_text(topic)
    # A later refresh failure keeps the last successful web summary, with a notice.
    web.fail, writer.fail = True, True
    third = connect_vault(settings, writer, web_enabled=True, refresh_web=True, web_client=web)
    assert third["status"] == "partial"
    assert "Previous web content is retained" in read_text(topic)
    assert "[Attention reference]" in read_text(topic)


def test_unreadable_supporting_note_defers_topic_update_instead_of_withdrawing_claims(settings):
    source = note(settings, "one.md", "# One\n\nSelf-attention is a key concept for understanding tokens.")
    writer = FakeWriter()
    first = connect_vault(settings, writer, reviewer=approve)
    topic = settings.vault_path / first["topics"][0]["path"]
    before = read_text(topic)
    source.write_text(read_text(source) + "\n[[damaged\n")
    result = connect_vault(settings, writer)
    assert result["status"] == "partial"
    assert "Self-attention weights relevant token information." in read_text(topic)
    assert "Topic update pending" in read_text(topic)
    assert "Topic update pending" not in before
    assert "No substantive supporting passages remain" not in read_text(topic)


def test_literal_management_markers_in_code_are_not_owned_or_modified(settings):
    path = note(settings, "one.md", "# Example\n\nSelf-attention is a key concept for understanding tokens.\n\n"
                "```md\n<!-- mynd:topics:begin -->\n<!-- mynd:link:example:begin -->\n```\n\n"
                "Inline `<!-- mynd:summary:begin -->` is documentation too.\n")
    original = read_text(path)
    doc = parse_document(path, settings.vault_path)
    assert not doc.errors
    assert block_range(original, "topics") is None
    result = connect_vault(settings, FakeWriter(), reviewer=approve)
    assert result["status"] == "complete"
    assert "```md\n<!-- mynd:topics:begin -->\n<!-- mynd:link:example:begin -->\n```" in read_text(path)


def test_ambiguous_word_senses_are_verified_before_connecting(settings):
    path = note(settings, "electrical.md", "# Electrical\n\nSelf-attention is a key concept for understanding tokens.")
    writer = FakeWriter()
    def no_match(topic, sources):
        writer.called("match")
        return []
    writer.match = no_match
    result = connect_vault(settings, writer, reviewer=approve)
    assert result["status"] == "complete"
    assert not owned_links(read_text(path))
    assert not result["topics"]
    assert result["candidate_register"]["counts"] == {"waiting": 1}
    assert not MaintenanceStore(settings).items("topics")  # Verification now precedes approval.


def test_merge_review_reuses_topic_identity_and_does_not_create_duplicate_node(settings):
    note(settings, "one.md", "# One\n\nSelf-attention is a key concept for understanding tokens.")
    writer = FakeWriter()
    first = connect_vault(settings, writer, reviewer=approve)
    existing_id = first["topics"][0]["id"]
    # Force the grouping stage to propose a separate concept for explicit merging.
    note(settings, "two.md", "# Two\n\nSelf-attention is a key concept used for representing context.")
    def group(candidates, known):
        writer.called("consolidate")
        return [{"title": "Contextual attention", "definition": candidates[0]["definition"],
                 "aliases": ["self-attention"], "members": [c["id"] for c in candidates], "topic_id": ""}]
    writer.consolidate = group
    result = connect_vault(settings, writer, reviewer=lambda *_: Decision("merge", topic_id=existing_id))
    assert len(result["topics"]) == 1
    assert result["topics"][0]["id"] == existing_id
    assert len(list((settings.vault_path / settings.topics_folder).glob("*.md"))) == 1


def test_clean_diagnoses_ambiguous_emphasis_and_table_damage_without_rewriting(settings):
    path = note(settings, "broken.md", "# Broken\n\n**Unclosed strong text.\n\n"
                "| A | B |\n| --- | --- |\n| one | two | three |\n")
    original = read_text(path)
    result = clean_vault(settings, apply=True)
    rules = {f["rule"] for n in result["notes"] for f in n["findings"]}
    assert {"possible-unclosed-emphasis", "possible-table-columns"} <= rules
    assert read_text(path) == original


def test_balanced_markdown_links_and_raw_urls_are_never_inline_rewritten(settings):
    text = ('# Sources\n\nA [self-attention [reference]](https://example.org/a(b(c)) "Title") '
            'and <https://example.org/self-attention> are existing links.\n\n'
            'A plain https://example.org/self-attention URL is protected too.\n\n'
            'Self-attention weights contextual token information.\n')
    path = note(settings, "source.md", text)
    doc = parse_document(path, settings.vault_path)
    assert not doc.errors
    sources = passages(doc, "one")
    assignment = {"topic_id": "topic-one", "target": "20 Topics/Self-attention", "title": "Attention",
                  "mentions": [{"source": source.id, "term": "self-attention"} for source in sources]}
    rendered = connect_note(read_text(path), "one", [assignment])
    link = owned_links(rendered)[0]
    assert link.label == "Self-attention"
    assert '[self-attention [reference]](https://example.org/a(b(c)) "Title")' in rendered
    assert "<https://example.org/self-attention>" in rendered
    assert "https://example.org/self-attention URL" in rendered


def test_malformed_ordinary_markdown_link_is_reported(settings):
    path = note(settings, "source.md", "# Source\n\nSee [this source](broken/path\n")
    assert "markdown-link-malformed" in {e["rule"] for e in parse_document(path, settings.vault_path).errors}


def test_cleanup_preserves_markdown_link_titles_and_destination_angles(settings):
    note(settings, "Refs/New (abcdef123456).md", "The actual referenced paper.",
         "paper-placeholder", "doi-abcdef123456")
    path = note(settings, "source.md", '# Source\n\nSee [my display](<Refs/Old (abcdef123456).md> "Hover title").\n')
    clean_vault(settings, apply=True)
    assert '[my display](<Refs/New%20%28abcdef123456%29.md> "Hover title")' in read_text(path)


def test_bare_link_is_ambiguous_even_if_one_candidate_is_at_vault_root(settings):
    a = note(settings, "Name.md", "Some substantive root text.")
    b = note(settings, "Folder/Name.md", "Some substantive nested text.")
    path = note(settings, "source.md", "See [[Name]].")
    doc = parse_document(path, settings.vault_path)
    assert set(LinkIndex(settings.vault_path, [a, b, path]).resolve(doc, doc.links[0])) == {a, b}


def test_mathematics_is_evidence_but_is_never_inline_modified(settings):
    path = note(settings, "source.md", "# Formula\n\nThe attention computation is $h = \\sum_i x_i$ for each token.\n")
    doc = parse_document(path, settings.vault_path)
    assert "$h = \\sum_i x_i$" in passages(doc, "one")[0].text
    assert "sum_i" not in doc.inline_mask


def test_summary_subheadings_do_not_alter_heading_examples_in_code(settings):
    paper = PaperNote(settings.vault_path, "paper-abcdef123456", {"title": "Paper"},
                      settings.pdf_store_path / "paper.pdf",
                      summary="## Overview\n\n```md\n## Overview\n```\n")
    content = read_text(write_paper_note(paper))
    assert "### Overview\n" in content
    assert "```md\n## Overview\n```" in content


def test_read_dependency_edits_abort_even_if_that_source_was_not_being_written(settings):
    source = note(settings, "source.md", "# Source\n\nOriginal source evidence.")
    before = read_text(source)
    target = settings.vault_path / "topic.md"
    source.write_text("A human changed the supporting evidence during inference.")
    with pytest.raises(ValueError, match="Concurrent"):
        apply_changes(settings, [Change(target, None, "Generated topic")], "test",
                      dependencies={source: before})
    assert not target.exists()


def test_failed_local_refresh_marks_old_topic_stale_and_retries_only_missing_synthesis(settings):
    source = note(settings, "source.md", "# Source\n\nSelf-attention is a key concept for understanding tokens.")
    writer = FakeWriter()
    first = connect_vault(settings, writer, reviewer=approve)
    topic = settings.vault_path / first["topics"][0]["path"]
    source.write_text(read_text(source).replace("understanding tokens", "analyzing token relevance"))
    summarize = writer.summarize
    def unavailable(*args, **kwargs):
        writer.called("failed-summary")
        raise RuntimeError("Local model interrupted")
    writer.summarize = unavailable
    failed = connect_vault(settings, writer)
    assert failed["status"] == "partial"
    assert "Topic update pending" in read_text(topic)
    assert "Self-attention weights relevant token information." in read_text(topic)
    calls_before_retry = writer.calls.copy()
    writer.summarize = summarize
    retried = connect_vault(settings, writer)
    assert retried["status"] == "complete"
    assert writer.calls == [*calls_before_retry, "summary"]
    assert "Topic update pending" not in read_text(topic)
    assert "analyzing token relevance" in read_text(topic)


def test_course_block_regeneration_preserves_approved_links_and_personal_text(settings):
    path = settings.vault_path / "lecture.md"
    blocks = {"summary": "## Summary\n\nSelf-attention is a key concept for understanding tokens."}
    write_course_blocks(path, "lecture", "lecture-one", "Lecture", blocks)
    path.write_text(read_text(path).replace("_Add personal notes here; this area is never regenerated._",
                                          "My own observations stay exactly here."))
    connect_vault(settings, FakeWriter(), reviewer=approve)
    topics_body = block_range(read_text(path), "topics")[2]
    write_course_blocks(path, "lecture", "lecture-one", "Lecture", blocks)
    after = read_text(path)
    assert len(owned_links(after)) == 1
    assert block_range(after, "topics")[2] == topics_body
    assert "My own observations stay exactly here." in after


def test_semantic_match_does_not_turn_a_generic_word_into_an_inline_link(settings):
    path = note(settings, "source.md", "# Source\n\nThis mechanism weights relevant token information.")
    source = passages(parse_document(path, settings.vault_path), "one")[0]
    rendered = connect_note(read_text(path), "one", [{
        "topic_id": "topic-one", "target": "20 Topics/Attention", "title": "Self-attention",
        "names": ["Self-attention"], "mentions": [{"source": source.id, "term": "mechanism"}],
    }])
    assert not owned_links(rendered)
    assert "This mechanism weights relevant token information." in rendered
    assert "[[20 Topics/Attention|Self-attention]]" in rendered


def test_unreadable_link_target_is_reported_without_aborting_cleanup(settings):
    (settings.vault_path / "bad.md").write_bytes(b"\xff\xfe not UTF-8")
    note(settings, "source.md", "# Source\n\nSee [[bad#Heading]] for details.")
    result = clean_vault(settings)
    assert any(note["status"] == "skipped" for note in result["notes"])
    assert any(f["rule"] == "unreadable-link-target" for note in result["notes"] for f in note.get("findings", []))


def test_connect_retains_bad_grouping_for_review_and_reuses_discovery_on_retry(settings):
    a = note(settings, "one.md", "# One\n\nSelf-attention is a key concept for understanding tokens.")
    b = note(settings, "two.md", "# Two\n\nSelf-attention is a key concept for understanding context.")
    before = {path: path.read_bytes() for path in (a, b)}
    writer = FakeWriter()
    consolidate = writer.consolidate
    def invalid(candidates, known):
        writer.called("invalid-consolidation")
        raise InvalidModelOutput("Missing candidate slots: C1")
    writer.consolidate = invalid
    result = connect_vault(settings, writer)
    assert result["status"] == "partial" and result["changed"] == 0
    assert result["grouping"]["unresolved_candidates"] == 2
    assert len(result["pending"]) == 2
    assert all("retained unmerged" in proposal["grouping_warning"] for proposal in result["pending"])
    assert all(error["batch"].startswith("Grouping batch 1/1.") for error in result["errors"])
    assert len(MaintenanceStore(settings).items("analysis")) == 2
    writer.consolidate = consolidate
    writer.calls.clear()
    retried = connect_vault(settings, writer)
    assert retried["status"] == "pending_review"
    assert retried["cached"] == 2 and retried["analyzed"] == 0
    assert writer.calls == ["consolidate", "consolidate"]  # Only unfinished leaves, not the failed parent.
    assert not retried["errors"] and retried["grouping"]["unresolved_candidates"] == 0
    assert len(retried["pending"]) == 1 and not retried["pending"][0]["grouping_warning"]
    assert before == {path: path.read_bytes() for path in (a, b)}


def test_connect_can_review_valid_batches_despite_a_separate_grouping_failure(settings):
    for index in range(3):
        note(settings, f"{index}.md", f"# Note {index}\n\nSelf-attention is a key concept for understanding token {index}.")
    writer = FakeWriter()
    consolidate = writer.consolidate
    def selective(candidates, known):
        if len(candidates) > 1:
            raise InvalidModelOutput("Missing candidate")
        return consolidate(candidates, known)
    writer.consolidate = selective
    result = connect_vault(settings, writer, reviewer=approve)
    assert result["status"] == "complete" and result["changed"] == 4
    assert result["grouping"]["split_batches"] == 2
    writer.fail = True
    repeated = connect_vault(settings, writer)
    assert repeated["status"] == "complete" and repeated["changed"] == 0
    assert repeated["grouping"]["cached_batches"] == 1


def test_consolidation_version_change_does_not_repeat_note_discovery(settings, monkeypatch):
    note(settings, "one.md", "# One\n\nSelf-attention is a key concept for understanding tokens.")
    writer = FakeWriter()
    connect_vault(settings, writer)
    analysis_before = MaintenanceStore(settings).items("analysis")
    monkeypatch.setattr("mynd.connect.grouping.CONSOLIDATION_VERSION", "test-new-grouping-schema")
    writer.calls.clear()
    result = connect_vault(settings, writer)
    assert result["cached"] == 1 and result["analyzed"] == 0
    assert writer.calls == ["consolidate"]
    assert MaintenanceStore(settings).items("analysis") == analysis_before


def test_connect_progress_and_cli_report_grouping_without_polluting_json(settings, config_file):
    note(settings, "one.md", "# One\n\nSelf-attention is a key concept for understanding tokens.")
    writer = FakeWriter()
    messages = []
    writer.progress = messages.append
    connect_vault(settings, writer)
    assert any("Discovery note 1/1: one.md" in message for message in messages)
    assert any("Grouping batch 1/1: 1 candidates" in message for message in messages)
    writer.generator.close = lambda: None
    runner = CliRunner()
    with patch("mynd.cli.load_settings", return_value=settings), patch("mynd.cli.TopicWriter", return_value=writer), \
         patch("mynd.cli.build_text_generator", return_value=writer.generator):
        result = runner.invoke(app, ["connect", "-c", str(config_file), "--non-interactive", "--offline", "--json"])
        assert result.exit_code == 1
        assert json.loads(result.stdout)["grouping"]["cached_batches"] == 1
        report = runner.invoke(app, ["connect", "-c", str(config_file), "--non-interactive", "--offline"])
        assert "Grouping: 1/1 batches complete, 1 cached" in report.stdout
