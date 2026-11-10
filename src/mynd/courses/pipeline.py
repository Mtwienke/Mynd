"""Incremental slide ingestion with explicit human approval of new course assignments."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import httpx

from mynd.ai.course import CourseWriter
from mynd.ai.models import build_text_generator
from mynd.ai.runtime import RuntimeControl
from mynd.courses.conversion import convert_powerpoint, pptx_slide_count, validate_export
from mynd.courses.models import (
    PIPELINE_VERSION,
    Assignment,
    Course,
    CourseProposal,
    Lecture,
    records,
)
from mynd.files import atomic_write_text, sha256_file
from mynd.identity import note_filename, slugify
from mynd.ingestion.slides import extract_slide_pages
from mynd.settings import Settings
from mynd.storage import courses as repository
from mynd.storage.database import initialize_database
from mynd.vault.course_notes import write_course_note, write_lecture_note

Reviewer = Callable[[Path, CourseProposal, list[Course]], Assignment | None]


def _notify(message: str) -> None:
    print(f"[courses] {message}", file=sys.stderr, flush=True)


def _prepare_source(source: Path, digest: str, data: Path) -> tuple[Path, Path]:
    """Content-addressed working cache; never open the user's original in PowerPoint."""
    cache = data / "course_cache" / digest
    cache.mkdir(parents=True, exist_ok=True)
    original = cache / ("original" + source.suffix.lower())
    if not original.exists() or sha256_file(original) != digest:
        shutil.copy2(source, original)
    if sha256_file(original) != digest:
        raise ValueError("Source changed during ingestion; retry after it finishes saving")
    if source.suffix.lower() == ".pdf":
        return original, original
    pdf = cache / "slides.pdf"
    manifest = cache / "conversion.json"
    expected = pptx_slide_count(original)
    reusable = False
    if pdf.is_file() and manifest.is_file():
        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
            reusable = info.get("pdf_sha256") == sha256_file(pdf)
        except (ValueError, OSError):
            pass
    if not reusable:
        convert_powerpoint(original, pdf)
        atomic_write_text(manifest, json.dumps({"pdf_sha256": sha256_file(pdf)}))
    validate_export(pdf, expected)
    return original, pdf


def _copy_artifact(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(source, destination)
    elif sha256_file(source) != sha256_file(destination):
        # Do not overwrite a managed copy that the user may have annotated/edited.
        raise ValueError(f"Managed source differs from its archived original: {destination}")


def _fingerprint(course: Course, lectures: list[Lecture]) -> str:
    material = {
        "version": PIPELINE_VERSION,
        "metadata": [course.title, course.university, course.semester, course.lecturer,
                     course.language],
        "lectures": [{"id": item.id, "digest": item.digest, "title": item.title,
                      "language": item.language, "order": item.order, "status": item.status,
                      "sections": records(item.sections)} for item in lectures],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class _Batch:
    def __init__(self, settings: Settings, reviewer: Reviewer | None, verbose: bool,
                 regenerate: bool, writer: CourseWriter | None):
        self.settings = settings
        self.reviewer = reviewer
        self.verbose = verbose
        self.regenerate = regenerate
        self.writer = writer
        self.generator = None
        self.checked_model = writer is not None
        self.runtime = RuntimeControl(settings.mode_file)

    def get_writer(self) -> CourseWriter | None:
        if not self.checked_model:
            self.checked_model = True
            self.generator = build_text_generator(
                runtime_control=self.runtime, progress=_notify, verbose=self.verbose,
            )
            if self.generator.ensure_model():
                self.writer = CourseWriter(self.generator, _notify)
            else:
                _notify("Local model unavailable; new assignments need manual review, "
                        "and study generation remains pending")
        return self.writer

    def ingest(self, source: Path) -> dict:
        settings = self.settings
        digest = sha256_file(source)
        previous = repository.find_lecture(settings.database_path, source, digest)
        if previous and previous.digest == digest and previous.version == PIPELINE_VERSION:
            repository.remember_source(settings.database_path, source, previous.id)
            if (previous.original_path.is_file()
                    and sha256_file(previous.original_path) != digest):
                raise ValueError("Managed original was edited; refusing to overwrite it: "
                                 f"{previous.original_path}")
            if (previous.pdf_digest and previous.pdf_path.is_file()
                    and sha256_file(previous.pdf_path) != previous.pdf_digest):
                raise ValueError("Managed PDF was edited; refusing to overwrite it: "
                                 f"{previous.pdf_path}")
            if (previous.status == "ready" and not self.regenerate
                    and previous.original_path.is_file() and previous.pdf_path.is_file()):
                course = next(c for c in repository.list_courses(settings.database_path)
                              if c.id == previous.course_id)
                # Rendering from stored data also safely repairs a missing note, without inference.
                if not previous.note_path.exists():
                    write_lecture_note(settings.vault_path, course, previous)
                return {"status": "unchanged", "lecture_id": previous.id,
                        "note": previous.note_path, "filename": source.name}

        _notify(f"Preparing slides: {source.name}")
        original, pdf = _prepare_source(source, digest, settings.data_path)
        pages = extract_slide_pages(pdf)
        identifier = previous.id if previous else "lecture-" + uuid4().hex[:12]
        known = repository.list_courses(settings.database_path)
        writer = self.get_writer()
        proposal = {}
        if previous:
            course = next(c for c in known if c.id == previous.course_id)
            assignment = Assignment(course, previous.title, previous.language, previous.order)
            proposal = previous.proposal
        else:
            suggestion = CourseProposal(lecture_title=source.stem)
            if writer:
                try:
                    suggestion = writer.propose(pages, identifier, known)
                except ValueError as exc:
                    _notify(f"Course inference unavailable: {exc}; manual assignment required")
            if self.reviewer is None:
                return {"status": "pending_review", "filename": source.name,
                        "proposal": asdict(suggestion), "error": "Run interactively to review "
                        "course membership; no assignment or vault note has been written"}
            assignment = self.reviewer(source, suggestion, known)
            if assignment is None:
                return {"status": "pending_review", "filename": source.name,
                        "error": "Assignment was not confirmed"}
            proposal = asdict(suggestion)

        course = assignment.course
        if not assignment.title.strip() or not course.title.strip():
            raise ValueError("Confirmed course and lecture titles must not be empty")
        # Validate reviewed paths before copying any files or persisting membership.
        course.note_path.resolve().relative_to(settings.slides_library.resolve())
        sources = course.note_path.parent / "Sources"
        basename = f"{slugify(source.stem)} ({digest[:12]})"
        stored_original = sources / (basename + source.suffix.lower())
        stored_pdf = sources / (basename + ".pdf")
        _copy_artifact(original, stored_original)
        _copy_artifact(pdf, stored_pdf)
        repository.save_course(settings.database_path, course)
        reuse = (previous is not None and previous.digest == digest
                 and previous.version == PIPELINE_VERSION and not self.regenerate)
        lecture = Lecture(
            id=identifier, course_id=course.id, title=assignment.title, language=assignment.language,
            order=assignment.order, digest=digest, original_path=stored_original,
            pdf_path=stored_pdf,
            note_path=previous.note_path if previous else course.note_path.parent / note_filename(
                assignment.title, identifier),
            pages=pages, sections=previous.sections if reuse else (),
            proposal=proposal, pdf_digest=sha256_file(stored_pdf),
        )
        repository.save_lecture(settings.database_path, lecture, source)
        atomic_write_text(settings.data_path / "course_text" / f"{digest}.json",
                          json.dumps(records(pages), ensure_ascii=False, indent=2))
        try:
            if writer is None:
                raise ValueError("Local model unavailable; retry to generate study material")
            if not lecture.sections:
                lecture = replace(lecture, sections=writer.summarize(pages, identifier,
                                                                    assignment.language))
            try:
                questions = writer.questions(lecture.sections, lecture.language)
                lecture = replace(lecture, questions=questions, status="ready")
            except (ValueError, httpx.HTTPError, OSError, RuntimeError) as exc:
                lecture = replace(lecture, status="partial", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - per-document generation failure boundary
            lecture = replace(lecture, status="failed", error=str(exc))
            repository.save_lecture(settings.database_path, lecture, source)
            write_lecture_note(settings.vault_path, course, lecture, preserve_study=True)
            return {"status": "failed", "lecture_id": identifier, "filename": source.name,
                    "note": lecture.note_path, "error": str(exc)}
        write_lecture_note(settings.vault_path, course, lecture)
        repository.save_lecture(settings.database_path, lecture, source)
        result = {"status": lecture.status, "lecture_id": identifier,
                  "filename": source.name, "course_id": course.id, "note": lecture.note_path}
        if lecture.error:
            result["error"] = lecture.error
        return result

    def refresh_course(self, course: Course) -> dict | None:
        settings = self.settings
        lectures = repository.course_lectures(settings.database_path, course.id)
        if not lectures:
            return None
        languages = Counter(lecture.language for lecture in lectures if lecture.language
                            and lecture.language != "Unknown")
        if languages:
            course = replace(course, language=languages.most_common(1)[0][0])
        fingerprint = _fingerprint(course, lectures)
        if not self.regenerate and course.fingerprint == fingerprint and not course.error:
            if not course.note_path.exists():
                write_course_note(settings.vault_path, course, lectures)
            return None
        material = [lecture for lecture in lectures if lecture.sections]
        try:
            writer = self.get_writer()
            if writer is None:
                raise ValueError("Local model unavailable; course synthesis remains pending")
            sections = writer.synthesize(course, material)
            course = replace(course, sections=sections, questions=(), error="")
            try:
                course = replace(course, questions=writer.questions(
                    sections, course.language, cross_lecture=len(material) > 1,
                ), fingerprint=fingerprint)
            except (ValueError, httpx.HTTPError, OSError, RuntimeError) as exc:
                course = replace(course, error=str(exc), fingerprint="")
            write_course_note(settings.vault_path, course, lectures)
        except Exception as exc:  # noqa: BLE001 - per-course generation failure boundary
            course = replace(course, error=str(exc))
            write_course_note(settings.vault_path, course, lectures, preserve_study=True)
        repository.save_course(settings.database_path, course)
        return {"status": "course_partial" if course.error else "course_updated",
                "course_id": course.id, "note": course.note_path, "error": course.error}


def ingest_slides(settings: Settings, reviewer: Reviewer | None = None, *,
                  verbose: bool = False, regenerate: bool = False,
                  writer: CourseWriter | None = None) -> list[dict]:
    """Process PDF/PPTX files recursively, with no automatic approval of new assignments."""
    inbox, library = settings.slides_inbox.resolve(), settings.slides_library.resolve()
    if inbox.is_relative_to(library) or library.is_relative_to(inbox):
        raise ValueError("Slides inbox must be outside the managed 15 Slides library")
    if settings.data_path.resolve().is_relative_to(inbox):
        raise ValueError("Slides inbox must not contain Mynd's runtime/cache directory")
    if settings.inbox_path.resolve().is_relative_to(inbox):
        raise ValueError("Slides inbox must not contain or equal the paper inbox")
    settings.slides_inbox.mkdir(parents=True, exist_ok=True)
    initialize_database(settings.database_path)
    sources = sorted(path for path in settings.slides_inbox.rglob("*")
                     if path.is_file() and not path.is_symlink()
                     and path.suffix.lower() in {".pdf", ".pptx"}
                     and not path.name.startswith((".", "~$")))
    _notify(f"Found {len(sources)} slide file(s) in {settings.slides_inbox}")
    batch = _Batch(settings, reviewer, verbose, regenerate, writer)
    results = []
    try:
        for source in sources:
            try:
                result = batch.ingest(source)
            except Exception as exc:  # noqa: BLE001 - isolate failures per source
                result = {"status": "failed", "filename": source.name, "error": str(exc)}
            repository.record_job(settings.database_path, source, result["status"],
                                  result.get("error", ""))
            results.append(result)
            _notify(f"{source.name}: {result['status']}")
            if batch.verbose:
                batch.runtime.report_thermal_state(source.name, _notify)
        for course in repository.list_courses(settings.database_path):
            try:
                result = batch.refresh_course(course)
            except Exception as exc:  # noqa: BLE001 - isolate failures per course
                result = {"status": "course_partial", "course_id": course.id, "error": str(exc)}
            if result:
                results.append(result)
        return results
    finally:
        if batch.generator is not None:
            batch.generator.close()
