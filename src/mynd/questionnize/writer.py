from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from mynd.ai.base import TextGenerator
from mynd.contracts import PaperMetadata

QUESTION_COUNTS = {"main_idea": 3, "methodology": 4, "findings": 3}
TOTAL_QUESTIONS = sum(QUESTION_COUNTS.values())
_WHITESPACE = re.compile(r"\s+")
_JSON_SHAPE = (
    '{"main_idea": ["...", "...", "..."], '
    '"methodology": ["...", "...", "...", "..."], '
    '"findings": ["...", "...", "..."]}'
)


class QuestionGenerationError(ValueError):
    """Raised when a model response cannot be turned into the required questions."""


@dataclass(frozen=True)
class QuestionSet:
    """Ten questions grouped by the aspect of the paper they probe."""

    main_idea: tuple[str, ...]
    methodology: tuple[str, ...]
    findings: tuple[str, ...]

    def __post_init__(self) -> None:
        for field, expected in QUESTION_COUNTS.items():
            questions = getattr(self, field)
            if len(questions) != expected:
                raise QuestionGenerationError(
                    f"Expected {expected} {field} questions, received {len(questions)}"
                )
            if any(not question.strip() for question in questions):
                raise QuestionGenerationError(f"{field} questions must not be empty")

    @property
    def total(self) -> int:
        return len(self.main_idea) + len(self.methodology) + len(self.findings)

    @classmethod
    def from_payload(cls, payload: object) -> QuestionSet:
        """Validate the model's JSON payload and normalize each question."""
        if not isinstance(payload, dict):
            raise QuestionGenerationError("Model must return a JSON object")
        groups: dict[str, tuple[str, ...]] = {}
        for field, expected in QUESTION_COUNTS.items():
            raw_questions = payload.get(field)
            if not isinstance(raw_questions, list):
                raise QuestionGenerationError(f"Missing or invalid '{field}' question list")
            if len(raw_questions) != expected:
                raise QuestionGenerationError(
                    f"Expected {expected} {field} questions, received {len(raw_questions)}"
                )
            groups[field] = tuple(_clean_question(question) for question in raw_questions)
        return cls(**groups)

    def to_markdown(self) -> str:
        """Render the questions as Markdown subsections for an Obsidian note."""
        blocks = []
        for heading, questions in (
            ("Main idea", self.main_idea),
            ("Methodology", self.methodology),
            ("Findings", self.findings),
        ):
            numbered = "\n".join(
                f"{index}. {question}" for index, question in enumerate(questions, start=1)
            )
            blocks.append(f"### {heading}\n\n{numbered}")
        return "\n\n".join(blocks)


@dataclass
class QuestionWriter:
    """Create grounded study questions from an existing paper summary."""

    generator: TextGenerator

    def generate(
        self,
        summary: str,
        metadata: PaperMetadata | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> QuestionSet:
        """Return exactly ten questions derived only from the supplied summary."""
        report = progress or (lambda _message: None)
        source = summary.strip()
        if not source:
            raise QuestionGenerationError("Cannot generate questions from an empty summary")

        report(f"Generating {TOTAL_QUESTIONS} questions from summary")
        prompt = self._prompt(source, metadata or {})
        raw = self.generator.generate(prompt)
        questions = QuestionSet.from_payload(_decode_payload(raw))
        report(f"Generated {questions.total} questions")
        return questions

    @staticmethod
    def _prompt(summary: str, metadata: PaperMetadata) -> str:
        context = _metadata_context(metadata)
        return f"""You are creating study questions for a personal research library.
Use only the summary below. Do not introduce facts that are not present in it.

Create exactly {TOTAL_QUESTIONS} questions:
- {QUESTION_COUNTS['main_idea']} about the paper's main idea: the central problem,
  claim, or contribution.
- {QUESTION_COUNTS['methodology']} about the methodology: study design, data,
  procedures, measures, or analysis.
- {QUESTION_COUNTS['findings']} about specific findings: concrete results,
  evidence, or conclusions.

Return JSON only, with exactly this shape and no commentary:
{_JSON_SHAPE}

Each question must be a single sentence ending with a question mark.

Paper metadata:
{context or "Not provided"}

Summary:
{summary}
"""


def _decode_payload(raw: str) -> object:
    """Decode JSON, tolerating a Markdown fence or surrounding model prose."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise QuestionGenerationError(f"Model returned invalid JSON: {exc}") from exc


def _clean_question(value: object) -> str:
    if not isinstance(value, str):
        raise QuestionGenerationError("Each question must be a string")
    question = _WHITESPACE.sub(" ", value).strip().lstrip("#").strip()
    if not question:
        raise QuestionGenerationError("Questions must not be empty")
    return question


def _metadata_context(metadata: PaperMetadata) -> str:
    fields = ("title", "authors", "year", "venue", "doi")
    return "\n".join(
        f"{key}: {value}"
        for key in fields
        if (value := metadata.get(key)) not in (None, "", [])
    )
