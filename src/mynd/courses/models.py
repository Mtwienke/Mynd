from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

QUESTION_COUNTS = {"concepts": 3, "application": 4, "synthesis": 3}
PIPELINE_VERSION = "course-v1"


@dataclass(frozen=True)
class SlidePage:
    number: int
    text: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    lecture_id: str
    page: int
    quote: str


@dataclass(frozen=True)
class StudySection:
    heading: str
    text: str
    sources: tuple[Evidence, ...]


@dataclass(frozen=True)
class StudyQuestion:
    group: str
    question: str
    answer: str
    sources: tuple[Evidence, ...]


@dataclass(frozen=True)
class Course:
    id: str
    title: str
    note_path: Path
    university: str = ""
    semester: str = ""
    lecturer: str = ""
    language: str = ""
    sections: tuple[StudySection, ...] = ()
    questions: tuple[StudyQuestion, ...] = ()
    fingerprint: str = ""
    error: str = ""


@dataclass(frozen=True)
class CourseProposal:
    lecture_title: str
    course_title: str = ""
    course_id: str = ""
    university: str = ""
    semester: str = ""
    lecturer: str = ""
    language: str = ""
    order: str = ""
    evidence: tuple[Evidence, ...] = ()
    saved: bool = False


@dataclass(frozen=True)
class Assignment:
    course: Course
    title: str
    language: str
    order: str


@dataclass(frozen=True)
class Lecture:
    id: str
    course_id: str
    title: str
    language: str
    order: str
    digest: str
    original_path: Path
    pdf_path: Path
    note_path: Path
    pages: tuple[SlidePage, ...]
    sections: tuple[StudySection, ...] = ()
    questions: tuple[StudyQuestion, ...] = ()
    status: str = "pending"
    error: str = ""
    version: str = PIPELINE_VERSION
    proposal: dict[str, Any] = field(default_factory=dict)
    pdf_digest: str = ""


def evidence_from(data: dict) -> Evidence:
    return Evidence(data["lecture_id"], data["page"], data["quote"])


def sections_from(data: list[dict]) -> tuple[StudySection, ...]:
    return tuple(StudySection(x["heading"], x["text"], tuple(
        evidence_from(s) for s in x["sources"]
    )) for x in data)


def questions_from(data: list[dict]) -> tuple[StudyQuestion, ...]:
    return tuple(StudyQuestion(x["group"], x["question"], x["answer"], tuple(
        evidence_from(s) for s in x["sources"]
    )) for x in data)


def records(items: tuple) -> list[dict]:
    return [asdict(item) for item in items]


def natural_order(value: str) -> tuple:
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r"(\d+)", value))
