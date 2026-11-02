import json
from pathlib import Path

import pytest

from mynd.questionnize import (
    QuestionAnswer,
    QuestionGenerationError,
    QuestionSet,
    QuestionWriter,
)
from mynd.questionnize.note import (
    EXTRACTED_QUESTIONS_HEADING,
    questionnize_note,
    read_summary,
    write_questions,
)
from mynd.questionnize.service import resolve_note_path
from mynd.settings import Settings
from mynd.storage import repositories
from mynd.storage.database import initialize_database
from mynd.vault.note_writer import PaperNote, write_paper_note

SUMMARY = "The paper studies local-first research workflows and reports faster recall."


def _payload():
    return {
        "main_idea": [
            {
                "question": f"Main question {index}?",
                "answer": f"Main answer {index}.",
            }
            for index in range(1, 4)
        ],
        "methodology": [
            {
                "question": f"Method question {index}?",
                "answer": f"Method answer {index}.",
            }
            for index in range(1, 5)
        ],
        "findings": [
            {
                "question": f"Finding question {index}?",
                "answer": f"Finding answer {index}.",
            }
            for index in range(1, 4)
        ],
    }


class FakeGenerator:
    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


class FakeQuestionWriter:
    def __init__(self, questions: QuestionSet):
        self.questions = questions
        self.summaries: list[str] = []

    def generate(self, summary: str, metadata=None, progress=None) -> QuestionSet:
        self.summaries.append(summary)
        return self.questions


def _questions() -> QuestionSet:
    return QuestionSet(
        main_idea=(
            QuestionAnswer(
                "What problem does the paper address?", "It addresses slow recall."
            ),
            QuestionAnswer("What is the central claim?", "Local-first notes improve recall."),
            QuestionAnswer("Why does it matter?", "It affects research workflows."),
        ),
        methodology=(
            QuestionAnswer("How was the study designed?", "A controlled comparison."),
            QuestionAnswer("What data were used?", "Notes from twelve researchers."),
            QuestionAnswer("How were measures defined?", "Recall and completion time."),
            QuestionAnswer("How was the analysis performed?", "A paired analysis."),
        ),
        findings=(
            QuestionAnswer("What was the main result?", "Recall improved."),
            QuestionAnswer("Which evidence supports it?", "The comparison found a gain."),
            QuestionAnswer("What conclusion follows?", "Local-first notes help."),
        ),
    )


def test_generate_returns_grouped_questions_and_renders_markdown():
    generator = FakeGenerator(f"```json\n{json.dumps(_payload())}\n```")
    writer = QuestionWriter(generator)
    progress: list[str] = []

    questions = writer.generate(
        SUMMARY,
        {"title": "A Paper", "authors": ["Ada"], "year": 2026},
        progress=progress.append,
    )

    assert questions.main_idea[0].question == "Main question 1?"
    assert questions.main_idea[0].answer == "Main answer 1."
    assert len(questions.methodology) == 4
    assert questions.total == 10
    assert "A Paper" in generator.prompts[0]
    assert SUMMARY in generator.prompts[0]
    assert "exactly 10 questions" in generator.prompts[0]
    assert "concise answer" in generator.prompts[0]
    assert progress[0] == "Generating 10 questions and answers from summary"
    assert progress[-1] == "Generated 10 questions with answers"

    markdown = questions.to_markdown()
    assert "### Main idea" in markdown
    assert "### Methodology" in markdown
    assert "### Findings" in markdown
    assert markdown.count("\n1. ") == 3
    assert "**Answer:** Main answer 1." in markdown


def test_generate_tolerates_surrounding_model_prose():
    generator = FakeGenerator(f"Here are the questions:\n{json.dumps(_payload())}\nDone.")
    writer = QuestionWriter(generator)

    questions = writer.generate(SUMMARY)

    assert questions.total == 10


@pytest.mark.parametrize(
    ("field", "count"),
    [("main_idea", 4), ("methodology", 3), ("findings", 2)],
)
def test_generate_rejects_wrong_question_counts(field, count):
    payload = _payload()
    payload[field] = [f"Extra question {index}?" for index in range(count)]
    writer = QuestionWriter(FakeGenerator(json.dumps(payload)))

    with pytest.raises(QuestionGenerationError, match="Expected"):
        writer.generate(SUMMARY)


def test_generate_rejects_malformed_json():
    writer = QuestionWriter(FakeGenerator("not json"))

    with pytest.raises(QuestionGenerationError, match="invalid JSON"):
        writer.generate(SUMMARY)


def test_generate_rejects_empty_summary():
    writer = QuestionWriter(FakeGenerator(json.dumps(_payload())))

    with pytest.raises(QuestionGenerationError, match="empty summary"):
        writer.generate("   ")


def _paper_note(tmp_path: Path, summary: str = SUMMARY) -> Path:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    return write_paper_note(
        PaperNote(
            tmp_path / "vault",
            "paper-abcdef123456",
            {"title": "A Study", "authors": ["Ada"]},
            pdf,
            summary=summary,
        )
    )


def test_read_summary_uses_known_note_headings_as_boundaries(tmp_path):
    note = _paper_note(tmp_path, "## Overview\nCentral idea.\n## Findings\nA result.")

    summary = read_summary(note)

    assert "Central idea." in summary
    assert "A result." in summary
    assert "No abstract retrieved" not in summary


def test_write_questions_inserts_chapter_and_replaces_it_on_rerun(tmp_path):
    note = _paper_note(tmp_path)
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            f"## {EXTRACTED_QUESTIONS_HEADING}\n\n_No extracted questions yet._\n\n", ""
        ),
        encoding="utf-8",
    )

    write_questions(note, _questions())
    first = note.read_text(encoding="utf-8")
    write_questions(note, _questions())
    second = note.read_text(encoding="utf-8")

    assert f"## {EXTRACTED_QUESTIONS_HEADING}" in first
    assert "### Main idea" in first
    assert first.count(f"## {EXTRACTED_QUESTIONS_HEADING}") == 1
    assert second.count(f"## {EXTRACTED_QUESTIONS_HEADING}") == 1
    assert second.index(f"## {EXTRACTED_QUESTIONS_HEADING}") < second.index("## Abstract")


def test_questionnize_note_reads_summary_and_writes_questions(tmp_path):
    note = _paper_note(tmp_path)
    writer = FakeQuestionWriter(_questions())

    questions = questionnize_note(note, writer)

    assert questions.total == 10
    assert writer.summaries == [SUMMARY]
    assert "### Findings" in note.read_text(encoding="utf-8")


def test_questionnize_note_rejects_notes_without_a_usable_summary(tmp_path):
    note = _paper_note(tmp_path, summary="")
    note.write_text("## Summary\n\n_Summary unavailable._\n", encoding="utf-8")

    with pytest.raises(ValueError, match="No usable summary"):
        questionnize_note(note, FakeQuestionWriter(_questions()))


def test_resolve_note_path_accepts_paths_paper_ids_and_dois(tmp_path):
    initialize_database(tmp_path / "library.sqlite3")
    note = tmp_path / "paper.md"
    note.write_text("## Summary\nText", encoding="utf-8")
    repositories.save_ingestion(
        tmp_path / "library.sqlite3",
        "doi-abc",
        {"title": "A Paper", "doi": "10.1000/paper"},
        repositories.IngestedDocument(
            digest="digest",
            filename="paper.pdf",
            stored_path=tmp_path / "paper.pdf",
            text="text",
        ),
    )
    repositories.set_paper_note_path(tmp_path / "library.sqlite3", "doi-abc", note)
    settings = Settings(
        vault_path=tmp_path / "vault",
        data_path=tmp_path / "data",
        inbox_path=tmp_path / "inbox",
        pdf_store_path=tmp_path / "pdfs",
        database_path=tmp_path / "library.sqlite3",
    )

    assert resolve_note_path(settings, str(note)) == note
    assert resolve_note_path(settings, "doi-abc") == note
    assert resolve_note_path(settings, "10.1000/paper") == note

    with pytest.raises(ValueError, match="No ingested paper"):
        resolve_note_path(settings, "missing")
