import json
from types import SimpleNamespace

import pytest

from mynd.ai.topics import TopicWriter
from mynd.connect.progress import ConnectProgress, connect_reporting, label, names
from mynd.vault.markdown import Passage


def test_progress_counts_completed_units_not_the_inflight_item():
    messages = []
    progress = ConnectProgress(messages.append)
    for number, item in progress.track(2, "Discovery", ["first.md", "second.md"], "notes", str):
        progress(f"Calculating {item}")
        assert progress.state.done == number - 1
    assert any("Discovery 0% (0/2 notes)] first.md" in line for line in messages)
    assert any("Discovery 50% (1/2 notes)] second.md" in line for line in messages)
    assert "Discovery 100% (2/2 notes)" in messages[-1]
    assert progress.state is None


def test_empty_phases_and_interrupted_phases_are_truthful():
    messages = []
    progress = ConnectProgress(messages.append)
    assert list(progress.track(6, "Update topics", [], "topics", str)) == []
    assert any("100% (0/0 topics)" in line and "No work" in line for line in messages)
    messages.clear()
    with pytest.raises(KeyboardInterrupt), progress.phase(3, "Grouping", 10, "batches"):
        progress.advance(3)
        raise KeyboardInterrupt
    assert "30% (3/10 batches)" in messages[-1] and "Interrupted" in messages[-1]
    assert not any("100%" in line for line in messages)


def test_nested_subphase_restores_the_current_review_topic():
    messages = []
    progress = ConnectProgress(messages.append)
    with progress.phase(5, "Review", 2, "topics"):
        progress.focus("Large Language Models")
        with progress.phase(progress.step, "Edited breadth", 1, "candidates"):
            progress.focus("Artificial Intelligence")
            progress("Model request")
            progress.advance()
        progress("Awaiting review decision")
    assert any("Connect 5/8 | Edited breadth" in line and "Artificial Intelligence" in line for line in messages)
    assert any("Review 0% (0/2 topics)] Large Language Models" in line for line in messages)


def test_runtime_and_writer_callbacks_share_context_and_restore_on_failure():
    messages, runtime_messages = [], []
    callback, runtime_callback = messages.append, runtime_messages.append
    writer = SimpleNamespace(progress=callback, generator=SimpleNamespace(progress=runtime_callback))
    with pytest.raises(RuntimeError), connect_reporting(writer) as progress, \
         progress.phase(6, "Update topics", 3, "topics"):
        progress.focus("Evolutionary Algorithms")
        writer.progress("Synthesize vault topic")
        writer.generator.progress("Ollama response stats: generation_rate=12.5 tokens/s")
        raise RuntimeError("Interrupted backend")
    assert writer.progress is callback and writer.generator.progress is runtime_callback
    assert "Evolutionary Algorithms" in runtime_messages[0] and "Connect 6/8" in runtime_messages[0]
    assert "Synthesize vault topic" in messages[1]


def test_reporting_supports_writers_without_optional_progress_attributes():
    writer = SimpleNamespace(generator=SimpleNamespace(model="fake-local"))
    with connect_reporting(writer) as progress:
        progress("No configured sink")
    assert not hasattr(writer, "progress") and not hasattr(writer.generator, "progress")


def test_progress_labels_are_bounded_and_cannot_inject_terminal_control_codes():
    assert label("LoRA\n\x1b[31m\tadaptation") == "LoRA [31m adaptation"
    assert len(label("x" * 1000)) == 220
    assert names(["AI", "LLMs", "AI", "EAs", "PINNs"]) == "AI; LLMs; EAs (+1 more)"


class Generator:
    model = "fake-local"

    def __init__(self, responses):
        self.responses = list(responses)

    def ensure_model(self):
        return True

    def generate(self, prompt):
        return self.generate_structured(prompt, {})

    def generate_structured(self, prompt, schema):
        return json.dumps(self.responses.pop(0))


def test_matching_progress_identifies_topic_passage_batch_and_current_source_notes():
    messages = []
    writer = TopicWriter(Generator([{"mentions": []}, {"mentions": []}]), messages.append)
    passages = [Passage(f"source-{i}", f"note-{i}", f"note-{i}.md", "Summary", "x" * 3000, 0, 3000)
                for i in range(2)]
    writer.match({"title": "Large Language Models", "definition": "A concept.", "aliases": []}, passages)
    assert "Large Language Models; passage batch 1/2; notes: note-0.md" in messages[0]
    assert "Large Language Models; passage batch 2/2; notes: note-1.md" in messages[1]


def test_synthesis_progress_identifies_topic_reduction_pass_and_batch():
    messages = []
    responses = [
        {"paragraphs": [{"text": "First condensed evidence.", "sources": ["source-0"]}]},
        {"paragraphs": [{"text": "Second condensed evidence.", "sources": ["source-6"]}]},
        {"paragraphs": [{"text": "A combined synthesis.", "sources": ["source-0", "source-6"]}]},
    ]
    writer = TopicWriter(Generator(responses), messages.append)
    writer.summarize([{"id": f"source-{i}", "text": "x" * 700} for i in range(7)], "Evolutionary Algorithms")
    assert "Evolutionary Algorithms; reduction pass 1; batch 1/2" in messages[0]
    assert "Evolutionary Algorithms; reduction pass 1; batch 2/2" in messages[1]
    assert "Evolutionary Algorithms; reduction pass 2; batch 1/1" in messages[2]


def test_repair_progress_explains_the_retry_without_logging_source_bodies():
    messages = []
    writer = TopicWriter(Generator([{"mentions": "invalid"}, {"mentions": []}]), messages.append)
    source = Passage("source", "note", "one.md", "Summary", "PRIVATE_SOURCE_TEXT", 0, 19)
    writer.match({"title": "A topic", "definition": "A concept.", "aliases": []}, [source])
    assert any("validation failed" in line and "retrying once" in line for line in messages)
    assert all("PRIVATE_SOURCE_TEXT" not in line for line in messages)
