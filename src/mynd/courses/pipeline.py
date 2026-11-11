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
from mynd.courses.diagnostics import Diagnostics
from mynd.courses.metadata import (
    apply_assignment,
    save_reviewed_course,
    saved_proposal,
    validate_assignment,
)
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


def _prepare_source(source: Path, digest: str, data: Path, *,
                    debug: Callable[[str], None] | None = None) -> tuple[Path, Path]:
    """Content-addressed working cache; never open the user's original in PowerPoint."""
    cache = data / "course_cache" / digest
    debug = debug or (lambda _: None)
    debug(f"Source cache: {cache}")
    cache.mkdir(parents=True, exist_ok=True)
    original = cache / ("original" + source.suffix.lower())
    if not original.exists() or sha256_file(original) != digest:
        shutil.copy2(source, original)
        debug(f"Copied working original: {source} -> {original}")
    else:
        debug(f"Reusing cached original: {original}")
    if sha256_file(original) != digest:
        raise ValueError("Source changed during ingestion; retry after it finishes saving")
    if source.suffix.lower() == ".pdf":
        debug("Input is already PDF; PowerPoint conversion is not needed")
        return original, original
    pdf = cache / "slides.pdf"
    manifest = cache / "conversion.json"
    expected = pptx_slide_count(original)
    debug(f"PowerPoint contains {expected} slides; checking conversion cache: {manifest}")
    reusable = False
    if pdf.is_file() and manifest.is_file():
        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
            reusable = info.get("pdf_sha256") == sha256_file(pdf)
        except (ValueError, OSError) as exc:
            debug(f"Conversion cache could not be reused: {type(exc).__name__}: {exc}")
    if not reusable:
        debug("No valid converted PDF cache; starting local PowerPoint export")
        convert_powerpoint(original, pdf, debug=debug)
        atomic_write_text(manifest, json.dumps({"pdf_sha256": sha256_file(pdf)}))
        debug(f"Saved conversion manifest: {manifest}")
    else:
        debug(f"Reusing hash-verified converted PDF: {pdf}")
    validate_export(pdf, expected)
    debug(f"Validated slide-to-page mapping: {expected} slides/pages")
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
                 regenerate: bool, writer: CourseWriter | None, review_existing: bool = False):
        self.settings = settings
        self.reviewer = reviewer
        self.verbose = verbose
        self.regenerate = regenerate
        self.review_existing = review_existing
        self.writer = writer
        self.generator = None
        self.checked_model = writer is not None
        self.runtime = RuntimeControl(settings.mode_file)
        self.diagnostics = Diagnostics(verbose, _notify)

    def get_writer(self) -> CourseWriter | None:
        if not self.checked_model:
            self.checked_model = True
            with self.diagnostics.step("model_availability"):
                self.generator = build_text_generator(
                    runtime_control=self.runtime, progress=_notify, verbose=self.verbose,
                )
                self.diagnostics.log(f"Checking local model: {self.generator.model}")
                if self.generator.ensure_model():
                    self.writer = CourseWriter(self.generator, _notify, verbose=self.verbose)
                    self.diagnostics.log("Local model ready")
                else:
                    _notify("Local model unavailable; new assignments need manual review, "
                            "and study generation remains pending")
        return self.writer

    def ingest(self, source: Path) -> dict:
        settings = self.settings
        debug = self.diagnostics
        debug.failed_stage = ""
        debug.log(f"Processing source: {source.resolve()}; regenerate={self.regenerate}")
        with debug.step("hash_source"):
            digest = sha256_file(source)
            debug.log(f"Source SHA-256: {digest}")
        with debug.step("lookup_lecture"):
            previous = repository.find_lecture(settings.database_path, source, digest)
            debug.log(f"Stored lecture: {previous.id if previous else 'none'}")
        metadata_changed = False
        if previous and self.review_existing:
            with debug.step("review_assignment"):
                known = repository.list_courses(settings.database_path)
                course = next(c for c in known if c.id == previous.course_id)
                assignment = self.reviewer(source, saved_proposal(previous, course), known)
                if assignment is None:
                    return {"status": "skipped", "filename": source.name,
                            "lecture_id": previous.id}
            with debug.step("save_metadata"):
                updated = apply_assignment(settings, previous, assignment)
                metadata_changed = updated != previous or assignment.course != course
                previous = updated
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
                    with debug.step("write_lecture_note"):
                        debug.log(f"Recreating missing note from stored data: {previous.note_path}")
                        write_lecture_note(settings.vault_path, course, previous)
                debug.log("Skipping inference: unchanged, completed lecture; archived files verified")
                return {"status": "metadata_updated" if metadata_changed else "unchanged",
                        "lecture_id": previous.id,
                        "note": previous.note_path, "filename": source.name}

        _notify(f"Preparing slides: {source.name}")
        with debug.step("prepare_source"):
            original, pdf = _prepare_source(source, digest, settings.data_path, debug=debug.log)
        with debug.step("extract_pages"):
            pages = extract_slide_pages(pdf)
            debug.log(f"Extracted {len(pages)} pages, {sum(len(p.text) for p in pages):,} text "
                      f"characters; {sum(bool(p.warnings) for p in pages)} pages have warnings")
        identifier = previous.id if previous else "lecture-" + uuid4().hex[:12]
        with debug.step("load_courses"):
            known = repository.list_courses(settings.database_path)
            debug.log(f"Loaded {len(known)} confirmed course(s) from {settings.database_path}")
        writer = self.get_writer()
        proposal = {}
        if previous:
            course = next(c for c in known if c.id == previous.course_id)
            assignment = Assignment(course, previous.title, previous.language, previous.order)
            proposal = previous.proposal
            debug.log(f"Reusing confirmed assignment: lecture={previous.id}, course={course.id}")
        else:
            suggestion = CourseProposal(lecture_title=source.stem)
            if writer:
                try:
                    with debug.step("infer_course"):
                        suggestion = writer.propose(pages, identifier, known)
                        debug.log(f"Proposal validated with {len(suggestion.evidence)} source "
                                  "references; assignment has not been saved")
                except ValueError as exc:
                    _notify(f"Course inference unavailable: {exc}; manual assignment required")
            if self.reviewer is None:
                debug.log("No interactive reviewer: leaving assignment pending; no vault writes")
                return {"status": "pending_review", "filename": source.name,
                        "proposal": asdict(suggestion), "stage": "review_assignment",
                        "error": "Run interactively to review "
                        "course membership; no assignment or vault note has been written"}
            with debug.step("review_assignment"):
                debug.log("Awaiting human approval; no course/lecture record or note saved yet")
                assignment = self.reviewer(source, suggestion, known)
            if assignment is None:
                return {"status": "skipped", "filename": source.name}
            proposal = asdict(suggestion)

        course = assignment.course
        debug.log(f"Confirmed assignment: lecture={identifier}, course={course.id}")
        with debug.step("validate_assignment"):
            validate_assignment(settings, assignment)
        sources = course.note_path.parent / "Sources"
        basename = f"{slugify(source.stem)} ({digest[:12]})"
        stored_original = sources / (basename + source.suffix.lower())
        stored_pdf = sources / (basename + ".pdf")
        if previous and previous.digest == digest:
            # Membership/name corrections do not relocate archived evidence or break page links.
            stored_original, stored_pdf = previous.original_path, previous.pdf_path
        with debug.step("archive_sources"):
            debug.log(f"Archive original: {original} -> {stored_original}")
            _copy_artifact(original, stored_original)
            debug.log(f"Archive PDF: {pdf} -> {stored_pdf}")
            _copy_artifact(pdf, stored_pdf)
        with debug.step("save_course"):
            course = save_reviewed_course(settings, course)
            debug.log(f"Saved course record {course.id} to {settings.database_path}")
        reuse = (previous is not None and previous.digest == digest
                 and previous.version == PIPELINE_VERSION and not self.regenerate
                 and previous.language == assignment.language
                 and previous.title == assignment.title and previous.course_id == course.id)
        lecture = Lecture(
            id=identifier, course_id=course.id, title=assignment.title, language=assignment.language,
            order=assignment.order, digest=digest, original_path=stored_original,
            pdf_path=stored_pdf,
            note_path=previous.note_path if previous else course.note_path.parent / note_filename(
                assignment.title, identifier),
            pages=pages, sections=previous.sections if reuse else (),
            proposal=proposal, pdf_digest=sha256_file(stored_pdf),
        )
        with debug.step("save_pending_lecture"):
            repository.save_lecture(settings.database_path, lecture, source)
            debug.log(f"Saved lecture record {lecture.id}, status={lecture.status}")
        with debug.step("save_extracted_text"):
            text_path = settings.data_path / "course_text" / f"{digest}.json"
            atomic_write_text(text_path, json.dumps(records(pages), ensure_ascii=False, indent=2))
            debug.log(f"Saved page-aware extracted text: {text_path}")
        try:
            if writer is None:
                with debug.step("summarize_lecture"):
                    raise ValueError("Local model unavailable; retry to generate study material")
            if not lecture.sections:
                with debug.step("summarize_lecture"):
                    lecture = replace(lecture, sections=writer.summarize(pages, identifier,
                                                                        assignment.language))
                    debug.log(f"Generated {len(lecture.sections)} validated study sections")
            else:
                debug.log(f"Reusing {len(lecture.sections)} stored study sections")
            try:
                with debug.step("generate_lecture_questions"):
                    questions = writer.questions(lecture.sections, lecture.language)
                    lecture = replace(lecture, questions=questions, status="ready")
                    debug.log(f"Generated {len(questions)} validated answered questions")
            except (ValueError, httpx.HTTPError, OSError, RuntimeError) as exc:
                lecture = replace(lecture, status="partial", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - per-document generation failure boundary
            lecture = replace(lecture, status="failed", error=str(exc))
            with debug.step("save_failed_lecture"):
                repository.save_lecture(settings.database_path, lecture, source)
            with debug.step("write_lecture_note"):
                debug.log(f"Writing failure status, preserving existing study text: {lecture.note_path}")
                write_lecture_note(settings.vault_path, course, lecture, preserve_study=True)
            return {"status": "failed", "lecture_id": identifier, "filename": source.name,
                    "note": lecture.note_path, "error": str(exc),
                    "stage": debug.failed_stage or "summarize_lecture"}
        with debug.step("write_lecture_note"):
            debug.log(f"Writing Obsidian lecture note: {lecture.note_path}")
            write_lecture_note(settings.vault_path, course, lecture)
        with debug.step("save_lecture"):
            repository.save_lecture(settings.database_path, lecture, source)
            debug.log(f"Saved lecture {lecture.id}: status={lecture.status}, "
                      f"sections={len(lecture.sections)}, questions={len(lecture.questions)}")
        result = {"status": lecture.status, "lecture_id": identifier,
                  "filename": source.name, "course_id": course.id, "note": lecture.note_path}
        if lecture.error:
            result["error"] = lecture.error
            result["stage"] = debug.failed_stage
        return result

    def refresh_course(self, course: Course) -> dict | None:
        settings = self.settings
        debug = self.diagnostics
        debug.failed_stage = ""
        debug.log(f"Checking course synthesis: {course.title} [{course.id}]")
        with debug.step("load_course_lectures"):
            lectures = repository.course_lectures(settings.database_path, course.id)
            debug.log(f"Course has {len(lectures)} lecture(s); "
                      f"{sum(bool(item.sections) for item in lectures)} have usable study sections")
        if not lectures:
            if course.sections or course.questions or course.error or not course.note_path.exists():
                course = replace(course, sections=(), questions=(), fingerprint="", error="")
                write_course_note(settings.vault_path, course, [])
                repository.save_course(settings.database_path, course)
                return {"status": "course_updated", "course_id": course.id,
                        "note": course.note_path, "error": ""}
            return None
        languages = Counter(lecture.language for lecture in lectures if lecture.language
                            and lecture.language != "Unknown")
        if languages and not course.language:
            course = replace(course, language=languages.most_common(1)[0][0])
        fingerprint = _fingerprint(course, lectures)
        if not self.regenerate and course.fingerprint == fingerprint and not course.error:
            debug.log("Skipping course inference: input fingerprint unchanged")
            if not course.note_path.exists():
                with debug.step("write_course_note"):
                    debug.log(f"Recreating missing course note: {course.note_path}")
                    write_course_note(settings.vault_path, course, lectures)
            return None
        material = [lecture for lecture in lectures if lecture.sections]
        debug.log(f"Course fingerprint={fingerprint}; regeneration required")
        try:
            with debug.step("synthesize_course"):
                writer = self.get_writer()
                if writer is None:
                    raise ValueError("Local model unavailable; course synthesis remains pending")
                sections = writer.synthesize(course, material)
                debug.log(f"Generated {len(sections)} course synthesis sections")
            course = replace(course, sections=sections, questions=(), error="")
            try:
                with debug.step("generate_course_questions"):
                    course = replace(course, questions=writer.questions(
                        sections, course.language, cross_lecture=len(material) > 1,
                    ), fingerprint=fingerprint)
                    debug.log(f"Generated {len(course.questions)} answered course questions")
            except (ValueError, httpx.HTTPError, OSError, RuntimeError) as exc:
                course = replace(course, error=str(exc), fingerprint="")
            with debug.step("write_course_note"):
                debug.log(f"Writing Obsidian course overview: {course.note_path}")
                write_course_note(settings.vault_path, course, lectures)
        except Exception as exc:  # noqa: BLE001 - per-course generation failure boundary
            course = replace(course, error=str(exc))
            with debug.step("write_course_note"):
                debug.log(f"Writing failure status, preserving prior synthesis: {course.note_path}")
                write_course_note(settings.vault_path, course, lectures, preserve_study=True)
        with debug.step("save_course"):
            repository.save_course(settings.database_path, course)
            debug.log(f"Saved course {course.id}: sections={len(course.sections)}, "
                      f"questions={len(course.questions)}, error={bool(course.error)}")
        return {"status": "course_partial" if course.error else "course_updated",
                "course_id": course.id, "note": course.note_path, "error": course.error,
                **({"stage": debug.failed_stage} if course.error else {})}


def ingest_slides(settings: Settings, reviewer: Reviewer | None = None, *,
                  verbose: bool = False, regenerate: bool = False,
                  writer: CourseWriter | None = None, review_existing: bool = False) -> list[dict]:
    """Process PDF/PPTX files recursively, with no automatic approval of new assignments."""
    inbox, library = settings.slides_inbox.resolve(), settings.slides_library.resolve()
    if inbox.is_relative_to(library) or library.is_relative_to(inbox):
        raise ValueError("Slides inbox must be outside the managed 15 Slides library")
    if settings.data_path.resolve().is_relative_to(inbox):
        raise ValueError("Slides inbox must not contain Mynd's runtime/cache directory")
    if settings.inbox_path.resolve().is_relative_to(inbox):
        raise ValueError("Slides inbox must not contain or equal the paper inbox")
    if review_existing and reviewer is None:
        raise ValueError("Reviewing saved assignments requires an interactive terminal")
    batch = _Batch(settings, reviewer, verbose, regenerate, writer, review_existing)
    batch.diagnostics.log(f"Slides inbox: {inbox}; library: {library}; "
                          f"database: {settings.database_path}; runtime data: {settings.data_path}")
    batch.diagnostics.log(f"Inference mode={batch.runtime.mode()}, regenerate={regenerate}, "
                          f"interactive_review={reviewer is not None}")
    with batch.diagnostics.step("initialize_storage"):
        settings.slides_inbox.mkdir(parents=True, exist_ok=True)
        initialize_database(settings.database_path)
    with batch.diagnostics.step("discover_sources"):
        sources = sorted(path for path in settings.slides_inbox.rglob("*")
                         if path.is_file() and not path.is_symlink()
                         and path.suffix.lower() in {".pdf", ".pptx"}
                         and not path.name.startswith((".", "~$")))
    _notify(f"Found {len(sources)} slide file(s) in {settings.slides_inbox}")
    results = []
    try:
        for source in sources:
            try:
                result = batch.ingest(source)
            except Exception as exc:  # noqa: BLE001 - isolate failures per source
                if not batch.diagnostics.failed_stage:
                    batch.diagnostics.failure("process_source", exc)
                result = {"status": "failed", "filename": source.name, "error": str(exc),
                          "stage": batch.diagnostics.failed_stage or "process_source"}
            repository.record_job(settings.database_path, source, result["status"],
                                  result.get("error", ""))
            results.append(result)
            detail = f" ({result.get('stage', 'process_source')}): {result['error']}" \
                if result.get("error") else ""
            _notify(f"{source.name}: {result['status']}{detail}")
            if batch.verbose:
                batch.runtime.report_thermal_state(source.name, _notify)
        for course in repository.list_courses(settings.database_path):
            try:
                result = batch.refresh_course(course)
            except Exception as exc:  # noqa: BLE001 - isolate failures per course
                if not batch.diagnostics.failed_stage:
                    batch.diagnostics.failure("refresh_course", exc)
                result = {"status": "course_partial", "course_id": course.id, "error": str(exc),
                          "stage": batch.diagnostics.failed_stage or "refresh_course"}
            if result:
                results.append(result)
                if result.get("error"):
                    _notify(f"{course.title}: {result['status']} ({result['stage']}): {result['error']}")
        return results
    finally:
        if batch.generator is not None:
            with batch.diagnostics.step("close_model_client"):
                batch.generator.close()


def review_saved_slides(settings: Settings, reviewer: Reviewer, target: str | None = None) -> list[dict]:
    """Metadata-only review, including archived lectures no longer present in the inbox."""
    initialize_database(settings.database_path)
    lectures = repository.list_lectures(settings.database_path)
    if target:
        lectures = [item for item in lectures if item.id == target]
        if not lectures:
            raise ValueError(f"Unknown lecture ID: {target}")
    results = []
    for lecture in lectures:
        known = repository.list_courses(settings.database_path)
        course = next(c for c in known if c.id == lecture.course_id)
        assignment = reviewer(lecture.original_path, saved_proposal(lecture, course), known)
        if assignment is None:
            status = "skipped"
        else:
            updated = apply_assignment(settings, lecture, assignment)
            status = "metadata_updated" if updated != lecture or assignment.course != course \
                else "unchanged"
        results.append({"status": status, "lecture_id": lecture.id, "note": lecture.note_path})
    return results
