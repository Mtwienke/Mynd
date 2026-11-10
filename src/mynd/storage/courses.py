"""Course persistence, deliberately independent of the paper/document tables."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from mynd.courses.models import (
    Course,
    Lecture,
    SlidePage,
    natural_order,
    questions_from,
    records,
    sections_from,
)
from mynd.storage.database import connect


def list_courses(database: Path) -> list[Course]:
    with connect(database) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM courses ORDER BY title, id").fetchall()
    return [Course(
        id=row["id"], title=row["title"], note_path=Path(row["note_path"]),
        university=row["university"], semester=row["semester"], lecturer=row["lecturer"],
        language=row["language"], sections=sections_from(json.loads(row["sections_json"])),
        questions=questions_from(json.loads(row["questions_json"])),
        fingerprint=row["fingerprint"], error=row["error"],
    ) for row in rows]


def save_course(database: Path, course: Course) -> None:
    with connect(database) as db:
        db.execute("""
            INSERT INTO courses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, note_path=excluded.note_path,
                university=excluded.university, semester=excluded.semester,
                lecturer=excluded.lecturer, language=excluded.language,
                sections_json=excluded.sections_json, questions_json=excluded.questions_json,
                fingerprint=excluded.fingerprint, error=excluded.error
        """, (course.id, course.title, str(course.note_path), course.university,
              course.semester, course.lecturer, course.language,
              json.dumps(records(course.sections), ensure_ascii=False),
              json.dumps(records(course.questions), ensure_ascii=False),
              course.fingerprint, course.error))


def _lecture(row: sqlite3.Row) -> Lecture:
    return Lecture(
        id=row["id"], course_id=row["course_id"], title=row["title"],
        language=row["language"], order=row["order_key"], digest=row["sha256"],
        original_path=Path(row["original_path"]), pdf_path=Path(row["pdf_path"]),
        note_path=Path(row["note_path"]),
        pages=tuple(SlidePage(p["number"], p["text"], tuple(p["warnings"]))
                    for p in json.loads(row["pages_json"])),
        sections=sections_from(json.loads(row["sections_json"])),
        questions=questions_from(json.loads(row["questions_json"])),
        status=row["status"], error=row["error"], version=row["version"],
        proposal=json.loads(row["proposal_json"]),
        pdf_digest=row["pdf_sha256"],
    )


def find_lecture(database: Path, source: Path, digest: str) -> Lecture | None:
    """A known source path is a revision; a matching hash at a new path is a rename."""
    with connect(database) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("""
            SELECT l.* FROM lectures l JOIN lecture_sources s ON s.lecture_id=l.id
            WHERE s.source_path=?
        """, (str(source.resolve()),)).fetchone()
        if row is None:
            row = db.execute("SELECT * FROM lectures WHERE sha256=?", (digest,)).fetchone()
    return _lecture(row) if row else None


def course_lectures(database: Path, course_id: str) -> list[Lecture]:
    with connect(database) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM lectures WHERE course_id=?", (course_id,)).fetchall()
    return sorted((_lecture(row) for row in rows),
                  key=lambda item: (natural_order(item.order), natural_order(item.title), item.id))


def remember_source(database: Path, source: Path, lecture_id: str) -> None:
    with connect(database) as db:
        db.execute("INSERT INTO lecture_sources VALUES (?, ?) ON CONFLICT(source_path) "
                   "DO UPDATE SET lecture_id=excluded.lecture_id",
                   (str(source.resolve()), lecture_id))


def save_lecture(database: Path, lecture: Lecture, source: Path) -> None:
    with connect(database) as db:
        db.execute("""
            INSERT INTO lectures (id, course_id, title, language, order_key, sha256,
                original_path, pdf_path, note_path, pages_json, sections_json, questions_json,
                status, error, version, proposal_json, pdf_sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET course_id=excluded.course_id, title=excluded.title,
                language=excluded.language, order_key=excluded.order_key, sha256=excluded.sha256,
                original_path=excluded.original_path, pdf_path=excluded.pdf_path,
                note_path=excluded.note_path, pages_json=excluded.pages_json,
                sections_json=excluded.sections_json, questions_json=excluded.questions_json,
                status=excluded.status, error=excluded.error, version=excluded.version,
                proposal_json=excluded.proposal_json, pdf_sha256=excluded.pdf_sha256
        """, (lecture.id, lecture.course_id, lecture.title, lecture.language, lecture.order,
              lecture.digest, str(lecture.original_path), str(lecture.pdf_path),
              str(lecture.note_path), json.dumps(records(lecture.pages), ensure_ascii=False),
              json.dumps(records(lecture.sections), ensure_ascii=False),
              json.dumps(records(lecture.questions), ensure_ascii=False), lecture.status,
              lecture.error, lecture.version, json.dumps(lecture.proposal, ensure_ascii=False),
              lecture.pdf_digest))
        db.execute("INSERT INTO lecture_sources VALUES (?, ?) ON CONFLICT(source_path) "
                   "DO UPDATE SET lecture_id=excluded.lecture_id",
                   (str(source.resolve()), lecture.id))


def record_job(database: Path, source: Path, status: str, error: str = "") -> None:
    with connect(database) as db:
        db.execute("INSERT INTO course_ingestion_jobs(source_path, status, error) VALUES (?, ?, ?)",
                   (str(source.resolve()), status, error))
