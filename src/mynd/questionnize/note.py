from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from mynd.contracts import PaperMetadata
from mynd.files import atomic_write_text
from mynd.questionnize.writer import QuestionSet, QuestionWriter
from mynd.vault.connections import preserve_connections
from mynd.vault.safety import read_text

EXTRACTED_QUESTIONS_HEADING = "Extracted questions"
_NO_SUMMARY = "_Summary unavailable"
_NOTE_HEADINGS = (
    "Summary",
    EXTRACTED_QUESTIONS_HEADING,
    "Abstract",
    "My notes",
    "References",
    "Questions",
    "Reading log",
    "Provenance",
    "Related topics",
    "Related topics (Mynd)",
)
_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$")


def read_summary(note_path: Path) -> str:
    """Return the body of a paper note's Summary section."""
    return extract_section(note_path.read_text(encoding="utf-8"), "Summary")


def extract_section(content: str, heading: str) -> str:
    """Return one note section, stopping at the next known note heading.

    AI summaries contain their own level-two headings, so a generic "next ##"
    boundary would truncate the summary. Only headings rendered by the note
    writer are treated as section boundaries.
    """
    lines = content.splitlines()
    marker = f"## {heading}"
    start = next((index + 1 for index, line in enumerate(lines) if line.strip() == marker), None)
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        match = _HEADING.match(lines[index])
        if match and match.group("title").strip() in _NOTE_HEADINGS:
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def write_questions(note_path: Path, questions: QuestionSet) -> Path:
    """Insert or replace the extracted-questions chapter in a paper note."""
    content = read_text(note_path)
    if "<!-- mynd:" in content and re.search(r"(?m)^type: (lecture|course)\s*$", content):
        raise ValueError(
            "Course notes use slide-grounded questions and managed blocks. "
            "Regenerate them with `mynd ingest --kind slides --regenerate`, not paper questionnize."
        )
    updated = preserve_connections(content, _upsert_section(
        content, EXTRACTED_QUESTIONS_HEADING, questions.to_markdown()
    ))
    if read_text(note_path) != content:
        raise ValueError(f"Concurrent edit detected; refusing to overwrite {note_path}")
    atomic_write_text(note_path, updated)
    return note_path


def questionnize_note(
    note_path: Path,
    writer: QuestionWriter,
    metadata: PaperMetadata | None = None,
    progress: Callable[[str], None] | None = None,
) -> QuestionSet:
    """Generate questions from a note's summary and write them back to the note."""
    summary = read_summary(note_path)
    if not summary or summary.startswith(_NO_SUMMARY):
        raise ValueError(f"No usable summary found in {note_path}")
    questions = writer.generate(summary, metadata, progress=progress)
    write_questions(note_path, questions)
    return questions


def _upsert_section(content: str, heading: str, body: str) -> str:
    """Replace a section if present, otherwise insert it after the summary."""
    lines = content.splitlines()
    marker = f"## {heading}"
    start = next((index for index, line in enumerate(lines) if line.strip() == marker), None)
    suffix: list[str] = []
    if start is not None:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if _HEADING.match(lines[index]):
                end = index
                break
        suffix = lines[end:]
        prefix = lines[:start]
    else:
        insert_at = _insertion_index(lines)
        prefix = lines[:insert_at]
        suffix = lines[insert_at:]

    while prefix and not prefix[-1].strip():
        prefix.pop()
    result = list(prefix)
    if result:
        result.append("")
    result.extend([marker, "", *body.strip().splitlines()])
    if suffix:
        result.extend(["", *suffix])
    return "\n".join(result).rstrip() + "\n"


def _insertion_index(lines: list[str]) -> int:
    """Place the chapter after the summary and before the next standard section."""
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and match.group("title").strip() in (
            "Abstract",
            "My notes",
            "References",
            "Questions",
            "Reading log",
            "Provenance",
        ):
            return index
    return len(lines)
