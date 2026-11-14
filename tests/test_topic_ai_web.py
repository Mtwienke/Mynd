import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest

from mynd.ai.topics import InvalidModelOutput, TopicWriter
from mynd.connect.web import OllamaWeb, public_url
from mynd.vault.markdown import Passage


class Generator:
    model = "fake-local"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.ensure_calls = 0

    def ensure_model(self):
        self.ensure_calls += 1
        return True

    def generate(self, prompt):
        return self.generate_structured(prompt, {})

    def generate_structured(self, prompt, schema):
        self.calls.append((prompt, schema))
        value = self.responses.pop(0)
        return value if isinstance(value, str) else json.dumps(value)


def passage():
    return Passage("src-one", "note-one", "one.md", "Summary",
                   "Self-attention weights tokens based on relevant context.", 0, 56)


def candidate(source="src-one"):
    return {"title": "Self-attention", "definition": "A contextual attention mechanism.",
            "aliases": ["self-attention"], "sources": [source], "role": "central",
            "important": True, "rationale": "This concept explains contextual token representations."}


def test_topic_extraction_constrains_ids_and_uses_local_generator_lazily():
    generator = Generator([{"topics": [candidate()]}])
    writer = TopicWriter(generator)
    assert not generator.calls and not generator.ensure_calls
    result = writer.extract([passage()])
    assert result[0]["sources"] == ["src-one"]
    assert generator.ensure_calls == 1
    schema = generator.calls[0][1]
    assert schema["properties"]["topics"]["items"]["properties"]["sources"]["items"]["enum"] == ["src-one"]
    assert "untrusted DATA" in generator.calls[0][0]


def test_unknown_evidence_is_repaired_once_then_refused():
    generator = Generator([{"topics": [candidate("invented")]}, {"topics": [candidate("invented")]}])
    with pytest.raises(ValueError, match="failed validation"):
        TopicWriter(generator).extract([passage()])
    assert len(generator.calls) == 2


def test_malformed_json_can_be_repaired_without_losing_valid_source_constraints():
    generator = Generator(["not JSON", {"topics": [candidate()]}])
    result = TopicWriter(generator).extract([passage()])
    assert result
    assert "Previous response failed validation" in generator.calls[1][0]


def test_topic_matching_refuses_rewritten_quotations():
    generator = Generator([
        {"mentions": [{"source": "src-one", "term": "invented paraphrase", "supports": True}]},
        {"mentions": [{"source": "src-one", "term": "Self-attention", "supports": True}]},
    ])
    result = TopicWriter(generator).match({"title": "Attention", "definition": "A mechanism.",
                                           "aliases": []}, [passage()])
    assert result[0]["term"] == "Self-attention"
    assert len(generator.calls) == 2


@pytest.mark.parametrize(("response", "message"), [
    ({"assignments": {"C0": "C0"}}, "Missing candidate slots.*C1"),
    ({"assignments": {"C0": "C0", "C1": "C1", "C2": "C2"}}, "unexpected slots.*C2"),
    ({"assignments": {"C0": "C1", "C1": "C0"}}, "Invalid assignment C0"),
    ({"assignments": {"C0": "C0", "C1": "invented"}}, "Invalid assignment C1"),
    ({"assignments": {"C0": "T0", "C1": "C0"}}, "Invalid assignment C0"),
    ({"assignments": {"C0": "C0", "C1": ["C0", "C1"]}}, "Invalid assignment C1"),
    ({"assignments": []}, "assignments.*object"),
    ('{"assignments":{"C0":"C0","C1":"C0","C1":"C1"}}', "Duplicate JSON key: C1"),
    ({"groups": []}, "assignments.*object"),
])
def test_consolidation_rejects_missing_duplicate_forward_and_unknown_assignments(response, message):
    generator = Generator([response, response])
    with pytest.raises(InvalidModelOutput, match=message):
        TopicWriter(generator).consolidate([dict(candidate(), id=f"candidate-{i}") for i in range(2)], [])
    assert len(generator.calls) == 2


def test_consolidation_constructs_transitive_partition_using_short_required_ids():
    candidates = [dict(candidate(), id=f"candidate-{i}", title=f"Concept {i}") for i in range(4)]
    generator = Generator([{"assignments": {"C0": "C0", "C1": "C0", "C2": "C1", "C3": "C3"}}])
    groups = TopicWriter(generator).consolidate(candidates, [])
    assert [g["members"] for g in groups] == [["candidate-0", "candidate-1", "candidate-2"], ["candidate-3"]]
    assert groups[0]["title"] == "Concept 0" and "Concept 2" in groups[0]["aliases"]
    prompt, schema = generator.calls[0]
    slots = schema["properties"]["assignments"]
    assert slots["required"] == ["C0", "C1", "C2", "C3"]
    assert slots["properties"]["C2"]["enum"] == ["C0", "C1", "C2"]
    assert slots["additionalProperties"] is False
    assert '"id": "C0"' in prompt and "candidate-0" not in prompt


def test_consolidation_resolves_existing_topic_assignments_and_backward_chains():
    known = [{"id": "topic-existing", "title": "Self-attention", "definition": "A mechanism.", "aliases": []}]
    generator = Generator([{"assignments": {"C0": "T0", "C1": "C0", "C2": "T0"}}])
    groups = TopicWriter(generator).consolidate([dict(candidate(), id=f"candidate-{i}") for i in range(3)], known)
    assert len(groups) == 1
    assert groups[0]["topic_id"] == "topic-existing"
    assert groups[0]["members"] == ["candidate-0", "candidate-1", "candidate-2"]
    assert '"id": "T0"' in generator.calls[0][0]


def test_consolidation_repairs_missing_slot_without_changing_candidate_identity():
    generator = Generator([{"assignments": {"C0": "C0"}}, {"assignments": {"C0": "C0", "C1": "C0"}}])
    groups = TopicWriter(generator).consolidate([dict(candidate(), id=f"candidate-{i}") for i in range(2)], [])
    assert groups[0]["members"] == ["candidate-0", "candidate-1"]
    assert "Missing candidate slots: ['C1']" in generator.calls[1][0]


def test_singleton_without_existing_topics_needs_no_model_request():
    generator = Generator([])
    groups = TopicWriter(generator).consolidate([dict(candidate(), id="candidate-one")], [])
    assert groups[0]["members"] == ["candidate-one"] and groups[0]["topic_id"] == ""
    assert not generator.calls and not generator.ensure_calls


def test_duplicate_input_candidate_ids_are_refused_before_model_calls():
    generator = Generator([])
    with pytest.raises(ValueError, match="Duplicate candidate IDs"):
        TopicWriter(generator).consolidate([dict(candidate(), id="same")] * 2, [])
    assert not generator.calls


def test_synthesis_is_cited_and_never_accepts_unknown_source_ids():
    generator = Generator([
        {"paragraphs": [{"text": "An invented claim.", "sources": ["not-supplied"]}]},
        {"paragraphs": [{"text": "A supported overview.", "sources": ["src-one"]}]},
    ])
    result = TopicWriter(generator).summarize([{"id": "src-one", "text": passage().text}], "Attention")
    assert result == [{"text": "A supported overview.", "sources": ["src-one"]}]


def test_noncondensing_hierarchical_output_stops_instead_of_looping():
    output = {"paragraphs": [{"text": "x" * 1300, "sources": ["src-0"]} for _ in range(6)]}
    generator = Generator([output, output])
    material = [{"id": f"src-{i}", "text": "word " * 130} for i in range(20)]
    with pytest.raises(ValueError, match="shrink"):
        TopicWriter(generator).summarize(material, "Attention")
    assert len(generator.calls) == 2


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://localhost:11434/api/generate", "http://127.0.0.1/private",
    "http://192.168.1.1/", "http://[::1]/", "https://user:password@example.org/",
    "https://private.internal/", "javascript:alert(1)", "https://example.org/>bad",
    "http://localhost./", "http://private.internal./", "http://127.1/", "http://0x7f.0.0.1/",
    "http://8.8.8.8:9999/",
])
def test_web_fetch_rejects_nonpublic_or_unsafe_urls(url):
    assert not public_url(url)


def test_web_fetch_accepts_public_https_urls():
    assert public_url("https://en.wikipedia.org/wiki/Attention_(machine_learning)")


def test_ollama_web_sends_only_query_and_selected_urls_not_note_text():
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append((str(request.url), payload))
        assert request.headers["Authorization"] == "Bearer fake-key"
        if request.url.path.endswith("web_search"):
            assert set(payload) == {"query", "max_results"}
            return httpx.Response(200, json={"results": [
                {"url": "http://127.0.0.1/private", "title": "unsafe"},
                {"url": "https://example.org/attention", "title": "Attention"},
            ]})
        assert payload == {"url": "https://example.org/attention"}
        return httpx.Response(200, json={"title": "Attention reference",
                                        "content": "Public explanation of attention. " * 10})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    web = OllamaWeb("fake-key", client)
    result = web.research("Self-attention definition overview")
    assert len(calls) == 2
    assert len(result["sources"]) == 1
    assert result["sources"][0]["url"] == "https://example.org/attention"
    assert datetime.fromisoformat(result["fetched_at"]).tzinfo == UTC
    web.close()


def test_web_requires_an_explicit_api_key_without_outbound_requests():
    client = MagicMock()
    with pytest.raises(ValueError, match="OLLAMA_API_KEY"):
        OllamaWeb("", client).research("Attention")
    client.post.assert_not_called()


def test_search_snippets_alone_are_not_presented_as_read_pages():
    def handler(request):
        if request.url.path.endswith("web_search"):
            return httpx.Response(200, json={"results": [{"url": "https://example.org/",
                                                         "content": "A long but unverified snippet." * 20}]})
        return httpx.Response(429)
    web = OllamaWeb("fake-key", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match="No usable web pages"):
        web.research("Attention")
    web.close()


@pytest.mark.parametrize("payload", [
    {"topics": [None]}, {"topics": [[]]}, {"topics": ["not an object"]},
])
def test_nonobject_candidate_records_are_repaired_not_crashed(payload):
    generator = Generator([payload, {"topics": [candidate()]}])
    assert TopicWriter(generator).extract([passage()])
    assert len(generator.calls) == 2
