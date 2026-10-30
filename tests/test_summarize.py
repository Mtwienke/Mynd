import json

import pytest

from ai.summarize import SummaryWriter


class FakeGenerator:
    def __init__(self, outputs=None):
        self.prompts = []
        self.outputs = list(outputs or ["## Overview\nA summary"])

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.outputs:
            return self.outputs.pop(0)
        return "chunk note"

    def is_running(self) -> bool:
        return True

    def ensure_model(self) -> bool:
        return True


def test_summarize_single_chunk_calls_generator_once():
    generator = FakeGenerator(["## Overview\nDone"])
    writer = SummaryWriter(generator, chunk_chars=1000)
    progress = []

    result = writer.summarize("Short paper text", progress=progress.append)

    assert result == "## Overview\nDone"
    assert len(generator.prompts) == 1
    assert progress == ["Summarizing text part 1/1", "Generating final paper summary"]


def test_summarize_multiple_chunks_condenses():
    generator = FakeGenerator(["note one", "note two", "## Overview\nFinal"])
    writer = SummaryWriter(generator, chunk_chars=20, overlap_chars=2)

    result = writer.summarize("A" * 15 + "\n\n" + "B" * 15)

    assert result == "## Overview\nFinal"
    # two chunk prompts + one final prompt
    assert len(generator.prompts) == 3
    assert "note one" in generator.prompts[-1]
    assert "note two" in generator.prompts[-1]


def test_summarize_empty_text_raises():
    writer = SummaryWriter(FakeGenerator())
    with pytest.raises(ValueError, match="Cannot summarize empty"):
        writer.summarize("   ")


def test_summarize_includes_metadata_context():
    generator = FakeGenerator(["## Overview\nDone"])
    writer = SummaryWriter(generator)
    writer.summarize("Text", metadata={"title": "T", "authors": ["Ada"], "year": 2020})
    assert "title: T" in generator.prompts[0]
    assert "authors: ['Ada']" in generator.prompts[0]


def test_summary_writer_validates_chunk_settings():
    with pytest.raises(ValueError):
        SummaryWriter(FakeGenerator(), chunk_chars=0)
    with pytest.raises(ValueError):
        SummaryWriter(FakeGenerator(), chunk_chars=10, overlap_chars=10)


def test_annotate_references_applies_json_annotations():
    payload = json.dumps(
        [
            {
                "index": 0,
                "importance": "foundational",
                "similarity": "very similar",
                "directions": ["method", "theory"],
            }
        ]
    )
    generator = FakeGenerator([f"```json\n{payload}\n```"])
    writer = SummaryWriter(generator)
    references = [{"raw_reference": "Ref A"}]

    result = writer.annotate_references("paper text", references)

    assert result[0]["importance"] == "foundational"
    assert result[0]["similarity"] == "very similar"
    assert result[0]["direction"] == "method, theory"


def test_annotate_references_ignores_malformed_output():
    generator = FakeGenerator(["not json"])
    writer = SummaryWriter(generator)
    references = [{"raw_reference": "Ref A"}]

    result = writer.annotate_references("paper text", references)

    assert "importance" not in result[0]


def test_annotate_references_empty_returns_unchanged():
    generator = FakeGenerator()
    writer = SummaryWriter(generator)
    assert writer.annotate_references("text", []) == []
    assert generator.prompts == []


def test_annotate_references_ignores_non_list_model_output():
    from unittest.mock import MagicMock

    generator = MagicMock()
    generator.generate.return_value = '{"not": "a list"}'
    writer = SummaryWriter(generator)
    references = [{"raw_reference": "One"}]

    assert writer.annotate_references("paper text", references) == references
