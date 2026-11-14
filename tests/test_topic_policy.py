import json
from types import SimpleNamespace

import pytest

from mynd.ai.topic_policy import MINIMUM_SOURCES
from mynd.ai.topics import InvalidModelOutput, TopicWriter
from mynd.connect.sources import source_identities, support_count


class Generator:
    model = "test-local"

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def ensure_model(self):
        return True

    def generate(self, prompt):
        return self.generate_structured(prompt, {})

    def generate_structured(self, prompt, schema):
        self.calls.append((prompt, schema))
        return json.dumps(self.responses.pop(0))


def assessment(**changes):
    return {"breadth": "specialized", "kind": "concept", "reason": "A specific adaptation technique.",
            "broader": [{"title": "Large Language Models", "definition": "Large-scale language models.",
                         "aliases": ["LLMs"]}]} | changes


def topic():
    return {"id": "concept-lora", "title": "LoRA", "definition": "Low-rank adaptation.", "aliases": []}


def test_local_policy_schema_calibrates_breadth_and_exclusions_with_required_short_slots():
    generator = Generator([{"assessments": {"C0": assessment()}}])
    result = TopicWriter(generator).classify([topic()])
    assert result["concept-lora"]["breadth"] == "specialized"
    assert MINIMUM_SOURCES == {"umbrella": 1, "established": 2, "specialized": 3, "niche": 5}
    prompt, schema = generator.calls[0]
    assert schema["properties"]["assessments"]["required"] == ["C0"]
    assert "NEVER allowed topic nodes" in prompt and "BERT" in prompt
    assert "concept-lora" not in prompt and '"id": "C0"' in prompt


@pytest.mark.parametrize("response", [
    {"assessments": {}}, {"assessments": {"C1": assessment()}},
    {"assessments": {"C0": assessment(breadth="important")}},
    {"assessments": {"C0": assessment(kind="approved")}},
    {"assessments": {"C0": assessment(reason="")}},
    {"assessments": {"C0": assessment(broader=[{}, {}])}},
])
def test_missing_or_invalid_policy_output_is_repaired_once_then_refused(response):
    generator = Generator([response, response])
    with pytest.raises(InvalidModelOutput):
        TopicWriter(generator).classify([topic()])
    assert len(generator.calls) == 2


def original(name, kind="note", **metadata):
    return (SimpleNamespace(relative=name, kind=kind, metadata=metadata), [])


def test_original_source_counts_deduplicate_dois_and_declared_derivatives():
    notes = {
        "paper": original("Paper.md", "paper", work_id="paper-one", doi="10.1234/ONE"),
        "copy": original("Copy.md", "paper", work_id="paper-copy", doi="10.1234/one"),
        "summary": original("Summary.md", derived_from="[[Paper]]"),
        "excerpt": original("Excerpt.md", source_work_id="paper-one"),
        "lecture": original("Lecture.md", "lecture", work_id="lecture-one"),
        "overview": original("Course.md", "course"),
        "personal": original("Personal.md"),
    }
    identities = source_identities(notes)
    assert identities["paper"] == identities["copy"] == identities["summary"] == identities["excerpt"]
    catalog = {key: SimpleNamespace(note_id=key) for key in notes}
    assert support_count(notes, catalog, identities) == 3  # Paper, lecture, original personal note.
    assert identities["overview"] is None


def test_multisource_summaries_and_circular_provenance_do_not_manufacture_support():
    notes = {"a": original("A.md", derived_from="[[B]]"),
             "b": original("B.md", derived_from="[[A]]"),
             "summary": original("Summary.md", derived_from=["[[One]]", "[[Two]]"])}
    assert all(value is None for value in source_identities(notes).values())
