from collections import Counter
from types import SimpleNamespace

import pytest

from mynd.ai.topic_policy import MINIMUM_SOURCES
from mynd.connect import Decision, connect_vault
from mynd.connect.policy import candidate_register_path
from mynd.settings import load_settings
from mynd.storage.maintenance import MaintenanceStore
from mynd.vault.managed import owned_links


@pytest.fixture
def settings(config_file):
    result = load_settings(config_file)
    result.vault_path.mkdir()
    return result


def note(settings, number, text="LoRA adapts low-rank parameters for language modelling.", metadata=""):
    path = settings.vault_path / f"source-{number}.md"
    path.write_text(f"---\ntype: note\n{metadata}---\n\n# Source {number}\n\n{text}\n")
    return path


class Writer:
    version = "policy-test-v1"

    def __init__(self, breadth="specialized", kind="concept", broader=False):
        self.generator = SimpleNamespace(model="fake-local")
        self.breadth, self.kind, self.broader = breadth, kind, broader
        self.calls = Counter()
        self.fail = False

    def called(self, name):
        assert not self.fail, f"Unexpected model request: {name}"
        self.calls[name] += 1

    def extract(self, sources):
        self.called("extract")
        return [{"id": "candidate-" + p.id, "title": "LoRA", "definition": "Low-rank adaptation.",
                 "aliases": [], "sources": [p.id], "role": "mention" if "passing" in p.text else "central",
                 "important": True, "rationale": "Very important, but this must not override breadth."}
                for p in sources if "LoRA" in p.text]

    def consolidate(self, candidates, known):
        self.called("consolidate")
        return [{"title": "LoRA", "definition": "Low-rank adaptation.", "aliases": [], "topic_id": "",
                 "members": [c["id"] for c in candidates]}]

    def classify(self, topics):
        self.called("classify")
        return {topic["id"]: {
            "breadth": "umbrella" if topic["title"] == "Large Language Models" else self.breadth,
            "kind": "concept" if topic["title"] == "Large Language Models" else self.kind,
            "reason": "A broad subject." if topic["title"] == "Large Language Models" else "A particular technique.",
            "broader": [{"title": "Large Language Models", "definition": "Large-scale language modelling.",
                         "aliases": ["LLMs"]}] if self.broader and topic["title"] == "LoRA" else [],
        } for topic in topics}

    def match(self, topic, sources):
        self.called("match")
        return [{"source": p.id, "term": "LoRA", "supports": "passing" not in p.text}
                for p in sources if "LoRA" in p.text and (topic["title"] == "LoRA" or "language modelling" in p.text)]

    def summarize(self, material, title, *, web=False):
        self.called("summary")
        return [{"text": "A supported conceptual overview.", "sources": [item["id"] for item in material]}]


@pytest.mark.parametrize("breadth", list(MINIMUM_SOURCES))
@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_breadth_thresholds_are_hard_floors_even_when_important(settings, breadth, count):
    for number in range(count):
        note(settings, number)
    result = connect_vault(settings, Writer(breadth), dry_run=True)
    required = MINIMUM_SOURCES[breadth]
    assert bool(result["pending"]) == (count >= required)
    entry = result["candidate_register"]["items"][0]
    assert entry["source_count"] == count and entry["minimum_sources"] == required
    assert entry["status"] == ("eligible" if count >= required else "waiting")
    assert result["changed"] == 0
    assert not settings.data_path.exists() and not settings.database_path.exists()


@pytest.mark.parametrize("kind", ["model_family", "dataset", "benchmark", "metadata"])
def test_excluded_kinds_never_reach_review_regardless_of_support(settings, kind):
    for number in range(6):
        note(settings, number)
    def forbidden_review(*_):
        pytest.fail("Excluded candidates must never interrupt review")
    result = connect_vault(settings, Writer("umbrella", kind), reviewer=forbidden_review)
    assert not result["pending"] and not result["topics"]
    assert result["candidate_register"]["counts"] == {"excluded": 1}


def test_saved_candidate_promotes_when_new_original_source_arrives_without_rediscovery(settings):
    for number in range(2):
        note(settings, number)
    writer = Writer()
    first = connect_vault(settings, writer)
    path = candidate_register_path(settings)
    assert path.exists() and not path.is_relative_to(settings.vault_path)
    assert first["candidate_register"]["counts"] == {"waiting": 1}
    text = path.read_text()
    assert "Original sources: **2/3**" in text and "Missing: **1**" in text and "source-0.md" in text
    note(settings, 2)
    writer.calls.clear()
    second = connect_vault(settings, writer)
    assert second["cached"] == 2 and second["analyzed"] == 1
    assert len(second["pending"]) == 1 and second["pending"][0]["source_count"] == 3
    assert writer.calls["extract"] == 1 and not writer.calls["classify"]
    before = path.read_bytes(), path.stat().st_mtime_ns
    writer.fail = True
    connect_vault(settings, writer)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_broader_fallback_requires_verified_context_not_just_taxonomy(settings):
    note(settings, 0)
    result = connect_vault(settings, Writer(broader=True))
    assert [proposal["title"] for proposal in result["pending"]] == ["Large Language Models"]
    records = {entry["title"]: entry for entry in result["candidate_register"]["items"]}
    assert records["LoRA"]["status"] == "waiting" and records["LoRA"]["minimum_sources"] == 3
    assert records["Large Language Models"]["origin_candidates"] == [records["LoRA"]["id"]]
    # The title/definition still suggest a taxonomy relationship, but the source
    # no longer provides language-model context. No parent may qualify.
    note(settings, 0, "LoRA is discussed here solely as an electrical measurement abbreviation.")
    result = connect_vault(settings, Writer(broader=True))
    assert not result["pending"]
    records = {entry["title"]: entry for entry in result["candidate_register"]["items"]}
    assert records["Large Language Models"]["source_count"] == 0


def test_approved_broader_fallback_links_source_without_narrow_node_or_false_alias(settings):
    source = note(settings, 0)
    result = connect_vault(settings, Writer(broader=True), reviewer=lambda *_: Decision("approve"))
    assert result["status"] == "complete"
    assert len(result["topics"]) == 1 and "Large Language Models" in result["topics"][0]["path"]
    text = source.read_text()
    assert "Large Language Models" in text and not owned_links(text)
    assert "LoRA adapts" in text  # A narrow name is never relabeled as an umbrella alias.
    assert result["candidate_register"]["counts"] == {"approved": 1, "waiting": 1}


def test_derived_notes_and_multiple_passages_do_not_inflate_independent_support(settings):
    note(settings, 0, metadata="work_id: paper-one\n")
    note(settings, 1, metadata='derived_from: "[[source-0]]"\n')
    note(settings, 2, metadata="source_work_id: paper-one\n")
    result = connect_vault(settings, Writer("established"))
    assert not result["pending"]
    assert result["candidate_register"]["items"][0]["source_count"] == 1


def test_passing_mentions_never_supply_creation_support(settings):
    for number in range(6):
        note(settings, number, "A passing LoRA mention without an explanation.")
    result = connect_vault(settings, Writer("umbrella"))
    assert not result["pending"] and result["candidate_register"]["items"][0]["source_count"] == 0


def test_review_cannot_raise_specificity_and_approve_below_new_threshold(settings):
    note(settings, 0)
    with pytest.raises(ValueError, match="below its original-source threshold"):
        connect_vault(settings, Writer("umbrella"), reviewer=lambda *_: Decision("approve", breadth="niche"))
    assert not MaintenanceStore(settings).items("topics")
    assert not (settings.vault_path / settings.topics_folder).exists()


def test_explicit_reclassification_changes_breadth_but_cannot_override_exclusion(settings):
    note(settings, 0)
    writer = Writer()
    first = connect_vault(settings, writer)
    identifier = first["candidate_register"]["items"][0]["id"]
    second = connect_vault(settings, writer, breadth_overrides={identifier: "umbrella"})
    assert len(second["pending"]) == 1
    assert second["pending"][0]["minimum_sources"] == 1
    writer.kind = "model_family"
    writer.version = "changed-kind-assessment"
    excluded = connect_vault(settings, writer, breadth_overrides={identifier: "umbrella"})
    assert not excluded["pending"] and excluded["candidate_register"]["counts"] == {"excluded": 1}


def test_removed_sources_make_candidates_inactive_without_forgetting_the_concept(settings):
    source = note(settings, 0)
    writer = Writer()
    first = connect_vault(settings, writer)
    source.unlink()
    writer.fail = True
    second = connect_vault(settings, writer)
    assert second["candidate_register"]["counts"] == {"inactive": 1}
    assert second["candidate_register"]["items"][0]["id"] == first["candidate_register"]["items"][0]["id"]
    assert second["candidate_register"]["items"][0]["source_count"] == 0


def test_candidate_register_preserves_personal_content_and_refuses_edited_generated_body(settings):
    note(settings, 0)
    writer = Writer()
    connect_vault(settings, writer)
    path = candidate_register_path(settings)
    path.write_text(path.read_text() + "\n## Personal observations\n\nKeep these exact words.\n")
    connect_vault(settings, writer)
    assert "Keep these exact words." in path.read_text()
    path.write_text(path.read_text().replace("A particular technique.", "My edited classification."))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="edited"):
        connect_vault(settings, writer)
    assert path.read_bytes() == before


def test_failed_assessment_retains_candidate_without_proposing_or_approving_it(settings):
    note(settings, 0)
    writer = Writer("umbrella")
    def unavailable(_):
        raise RuntimeError("Local model unavailable")
    writer.classify = unavailable
    result = connect_vault(settings, writer)
    assert result["status"] == "partial" and not result["pending"]
    assert result["candidate_register"]["counts"] == {"assessment_pending": 1}


def legacy_topic(settings):
    topic = {"id": "topic-legacy", "title": "LoRA", "definition": "Low-rank adaptation.", "aliases": [],
             "query": "LoRA overview", "path": "20 Topics/LoRA.md", "adopted": False}
    store = MaintenanceStore(settings)
    store.put("topics", topic["id"], topic)
    store.save()
    return topic


def test_legacy_approval_without_a_file_cannot_create_a_below_threshold_node(settings):
    note(settings, 0)
    old = legacy_topic(settings)
    writer = Writer()
    consolidate = writer.consolidate
    def bind(candidates, known):
        return [dict(group, topic_id=old["id"]) for group in consolidate(candidates, known)]
    writer.consolidate = bind
    result = connect_vault(settings, writer)
    assert not result["topics"] and not result["pending"] and result["changed"] == 0
    assert result["legacy_approvals"][0]["id"] == old["id"]
    assert MaintenanceStore(settings).get("topics", old["id"]) == old
    assert not (settings.vault_path / old["path"]).exists()


def test_legacy_file_is_preserved_without_automatic_policy_migration(settings):
    old = legacy_topic(settings)
    path = settings.vault_path / old["path"]
    path.parent.mkdir()
    path.write_text("# My legacy topic\n\nPersonal writing must survive.\n")
    before = path.read_bytes()
    result = connect_vault(settings, Writer())
    assert result["changed"] == 0 and path.read_bytes() == before


def test_eligible_legacy_approval_requires_review_and_reuses_its_original_id(settings):
    for number in range(3):
        note(settings, number)
    old = legacy_topic(settings)
    writer = Writer()
    consolidate = writer.consolidate
    writer.consolidate = lambda candidates, known: [dict(group, topic_id=old["id"])
                                                   for group in consolidate(candidates, known)]
    pending = connect_vault(settings, writer)
    assert len(pending["pending"]) == 1 and pending["changed"] == 0
    approved = connect_vault(settings, writer, reviewer=lambda *_: Decision("approve"))
    assert len(approved["topics"]) == 1 and approved["topics"][0]["id"] == old["id"]
    assert not approved["legacy_approvals"]
