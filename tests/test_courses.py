import sqlite3
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pymupdf
import pytest

from mynd.courses.models import (
    QUESTION_COUNTS,
    Assignment,
    Course,
    CourseProposal,
    Evidence,
    StudyQuestion,
    StudySection,
)
from mynd.courses.pipeline import ingest_slides
from mynd.settings import Settings, load_settings
from mynd.storage import courses as repository
from mynd.storage.database import initialize_database
from mynd.vault.course_notes import write_course_note, write_lecture_note

QUOTE = "Optimization minimizes an objective function subject to constraints."


@pytest.fixture
def settings(tmp_path):
    return Settings(vault_path=tmp_path / "obsidian", data_path=tmp_path / "data",
                    inbox_path=tmp_path / "inbox", pdf_store_path=tmp_path / "papers",
                    database_path=tmp_path / "library.sqlite3", use_grobid=False)


def make_pdf(path, text=QUOTE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((30, 30), text)
        pdf.save(path)
    return path


class Writer:
    def __init__(self):
        self.proposals = 0
        self.summaries = 0
        self.quizzes = 0
        self.syntheses = 0
        self.fail_questions_once = False
        self.fail_summarize = False

    def propose(self, pages, lecture_id, known):
        self.proposals += 1
        return CourseProposal("Optimization", language="English",
                              evidence=(Evidence(lecture_id, 1, pages[0].text),))

    def summarize(self, pages, lecture_id, language):
        self.summaries += 1
        if self.fail_summarize:
            raise ValueError("Model failed during summary generation")
        return (StudySection("Objective", pages[0].text,
                             (Evidence(lecture_id, 1, pages[0].text),)),)

    def questions(self, sections, language, *, cross_lecture=False):
        self.quizzes += 1
        if self.fail_questions_once:
            self.fail_questions_once = False
            raise ValueError("Not enough supported questions")
        sources = tuple(source for section in sections for source in section.sources)
        return tuple(StudyQuestion(group, f"{group} question {i}?", "A supported answer.", sources)
                     for group, count in QUESTION_COUNTS.items() for i in range(count))

    def synthesize(self, course, lectures):
        self.syntheses += 1
        if not lectures:
            raise ValueError("No completed lecture study material")
        return tuple(section for lecture in lectures for section in lecture.sections)


def reviewer(settings, assignments=None):
    def assign(source, proposal, known):
        if assignments is not None:
            assignments.append((source, proposal, known))
        course = known[0] if known else Course(
            "course-confirmed", "Confirmed course",
            settings.slides_library / "Confirmed course" / "Course overview.md",
            language="English",
        )
        return Assignment(course, proposal.lecture_title, proposal.language, source.stem)
    return assign


def test_full_ingestion_creates_linked_lecture_and_course_nodes(settings):
    source = make_pdf(settings.slides_inbox / "01.pdf")
    before = source.read_bytes()
    result = ingest_slides(settings, reviewer(settings), writer=Writer())
    assert [item["status"] for item in result] == ["ready", "course_updated"]
    course = repository.list_courses(settings.database_path)[0]
    lecture = repository.course_lectures(settings.database_path, course.id)[0]
    note = lecture.note_path.read_text()
    hub = course.note_path.read_text()
    assert "Course overview|Confirmed course" in note
    assert "#page=1" in note
    assert note.count("**Answer:**") == 10
    assert "Connections and synthesis" in note
    assert "Course revision quiz" in hub
    assert lecture.note_path.stem in hub
    assert "Only one lecture" in hub
    assert source.read_bytes() == before
    assert lecture.original_path.read_bytes() == before
    assert lecture.pdf_path.is_relative_to(settings.slides_library)
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_unreviewed_slides_write_no_course_nodes_or_assignments(settings):
    make_pdf(settings.slides_inbox / "misleading course folder" / "01.pdf")
    result = ingest_slides(settings, writer=Writer())
    assert result[0]["status"] == "pending_review"
    assert repository.list_courses(settings.database_path) == []
    assert not settings.slides_library.exists()
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM lectures").fetchone()[0] == 0
        assert db.execute("SELECT status FROM course_ingestion_jobs").fetchone()[0] == "pending_review"


def test_declined_review_is_not_remembered(settings):
    make_pdf(settings.slides_inbox / "01.pdf")
    result = ingest_slides(settings, lambda *_: None, writer=Writer())
    assert result[0]["status"] == "pending_review"
    assert not repository.list_courses(settings.database_path)


def test_unchanged_decks_skip_all_inference_and_review(settings):
    make_pdf(settings.slides_inbox / "01.pdf")
    writer = Writer()
    ingest_slides(settings, reviewer(settings), writer=writer)
    with patch("mynd.courses.pipeline.build_text_generator") as generator:
        result = ingest_slides(settings, lambda *_: pytest.fail("Review repeated"))
    assert [item["status"] for item in result] == ["unchanged"]
    generator.assert_not_called()


def test_missing_notes_are_recreated_without_inference(settings):
    make_pdf(settings.slides_inbox / "01.pdf")
    ingest_slides(settings, reviewer(settings), writer=Writer())
    course = repository.list_courses(settings.database_path)[0]
    lecture = repository.course_lectures(settings.database_path, course.id)[0]
    lecture.note_path.unlink()
    course.note_path.unlink()
    with patch("mynd.courses.pipeline.build_text_generator") as generator:
        ingest_slides(settings)
    generator.assert_not_called()
    assert lecture.note_path.exists() and course.note_path.exists()


def test_changed_deck_retains_identity_and_personal_notes(settings):
    source = make_pdf(settings.slides_inbox / "01.pdf")
    writer = Writer()
    ingest_slides(settings, reviewer(settings), writer=writer)
    course = repository.list_courses(settings.database_path)[0]
    first = repository.course_lectures(settings.database_path, course.id)[0]
    first.note_path.write_text(first.note_path.read_text() + "\n## My custom chapter\nPersonal insight.\n")
    course.note_path.write_text(course.note_path.read_text() + "\nPersonal course plan.\n")
    source.unlink()
    make_pdf(source, "A revised objective function is minimized with constraints.")
    result = ingest_slides(settings, writer=writer)
    revised = repository.course_lectures(settings.database_path, course.id)[0]
    assert result[0]["status"] == "ready"
    assert first.id == revised.id
    assert first.digest != revised.digest
    assert revised.note_path == first.note_path
    assert "Personal insight." in revised.note_path.read_text()
    assert "Personal course plan." in course.note_path.read_text()
    assert first.original_path.is_file()  # Keep previous revisions for old source references.
    assert writer.proposals == 1
    assert writer.summaries == 2 and writer.syntheses == 2


def test_renamed_source_does_not_duplicate_lecture(settings):
    source = make_pdf(settings.slides_inbox / "01.pdf")
    writer = Writer()
    ingest_slides(settings, reviewer(settings), writer=writer)
    source.rename(source.with_name("renamed.pdf"))
    result = ingest_slides(settings, writer=writer)
    assert result[0]["status"] == "unchanged"
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM lectures").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM lecture_sources").fetchone()[0] == 2
    assert writer.summaries == 1


def test_explicit_regeneration_preserves_notes_and_confirmed_membership(settings):
    make_pdf(settings.slides_inbox / "01.pdf")
    writer = Writer()
    ingest_slides(settings, reviewer(settings), writer=writer)
    ingest_slides(settings, lambda *_: pytest.fail("Membership should be remembered"),
                  writer=writer, regenerate=True)
    assert writer.proposals == 1
    assert writer.summaries == 2 and writer.syntheses == 2


def test_question_failure_retries_without_resummarizing_unchanged_deck(settings):
    make_pdf(settings.slides_inbox / "01.pdf")
    writer = Writer()
    writer.fail_questions_once = True
    result = ingest_slides(settings, reviewer(settings), writer=writer)
    assert result[0]["status"] == "partial"
    course = repository.list_courses(settings.database_path)[0]
    lecture = repository.course_lectures(settings.database_path, course.id)[0]
    assert "Questions unavailable" in lecture.note_path.read_text()
    result = ingest_slides(settings, writer=writer)
    assert result[0]["status"] == "ready"
    assert writer.summaries == 1


def test_bad_pdf_does_not_abort_other_decks(settings):
    bad = settings.slides_inbox / "00_broken.pdf"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a PDF")
    make_pdf(settings.slides_inbox / "01_valid.PDF")
    result = ingest_slides(settings, reviewer(settings), writer=Writer())
    assert [r["status"] for r in result][:2] == ["failed", "ready"]


def test_multiple_lectures_use_reviewed_membership_and_natural_order(settings):
    make_pdf(settings.slides_inbox / "lecture_10.pdf", QUOTE + " Ten.")
    make_pdf(settings.slides_inbox / "lecture_2.pdf", QUOTE + " Two.")
    assignments = []
    ingest_slides(settings, reviewer(settings, assignments), writer=Writer())
    course = repository.list_courses(settings.database_path)[0]
    lectures = repository.course_lectures(settings.database_path, course.id)
    assert [lecture.order for lecture in lectures] == ["lecture_2", "lecture_10"]
    assert len(assignments[1][2]) == 1
    assert "Only one lecture" not in course.note_path.read_text()


def test_offline_new_decks_remain_unassigned_and_generator_is_closed(settings):
    make_pdf(settings.slides_inbox / "01.pdf")
    generator = MagicMock()
    generator.ensure_model.return_value = False
    with patch("mynd.courses.pipeline.build_text_generator", return_value=generator):
        result = ingest_slides(settings)
    assert result[0]["status"] == "pending_review"
    assert not repository.list_courses(settings.database_path)
    generator.close.assert_called_once()


def test_offline_manual_assignment_archives_source_and_records_pending_generation(settings):
    make_pdf(settings.slides_inbox / "01.pdf")
    generator = MagicMock()
    generator.ensure_model.return_value = False
    with patch("mynd.courses.pipeline.build_text_generator", return_value=generator):
        result = ingest_slides(settings, reviewer(settings))
    assert result[0]["status"] == "failed"
    course = repository.list_courses(settings.database_path)[0]
    lecture = repository.course_lectures(settings.database_path, course.id)[0]
    assert lecture.original_path.exists()
    assert "Study material unavailable" in lecture.note_path.read_text()
    assert "pending" in course.note_path.read_text()


def test_failed_revision_marks_old_material_stale_and_retains_personal_notes(settings):
    source = make_pdf(settings.slides_inbox / "01.pdf")
    writer = Writer()
    ingest_slides(settings, reviewer(settings), writer=writer)
    course = repository.list_courses(settings.database_path)[0]
    old = repository.course_lectures(settings.database_path, course.id)[0]
    old.note_path.write_text(old.note_path.read_text() + "\nKeep my notes.\n")
    source.unlink()
    make_pdf(source, "A new revision with a different definition of optimization.")
    writer.fail_summarize = True
    result = ingest_slides(settings, writer=writer)
    assert result[0]["status"] == "failed"
    assert "may be stale" in old.note_path.read_text()
    assert "Keep my notes." in old.note_path.read_text()
    assert QUOTE in old.note_path.read_text()
    assert "may be stale" in course.note_path.read_text()


def test_managed_source_edits_are_never_overwritten(settings):
    source = make_pdf(settings.slides_inbox / "01.pdf")
    writer = Writer()
    ingest_slides(settings, reviewer(settings), writer=writer)
    course = repository.list_courses(settings.database_path)[0]
    lecture = repository.course_lectures(settings.database_path, course.id)[0]
    lecture.original_path.write_bytes(b"user annotated file")
    result = ingest_slides(settings, writer=writer, regenerate=True)
    assert result[0]["status"] == "failed"
    assert lecture.original_path.read_bytes() == b"user annotated file"
    assert source.is_file()


def test_damaged_management_markers_refuse_to_clobber_notes(settings):
    make_pdf(settings.slides_inbox / "01.pdf")
    writer = Writer()
    ingest_slides(settings, reviewer(settings), writer=writer)
    course = repository.list_courses(settings.database_path)[0]
    lecture = repository.course_lectures(settings.database_path, course.id)[0]
    damaged = lecture.note_path.read_text().replace("<!-- mynd:summary:end -->", "My edit")
    lecture.note_path.write_text(damaged)
    with pytest.raises(ValueError, match="missing/duplicated"):
        write_lecture_note(settings.vault_path, course, lecture)
    assert lecture.note_path.read_text() == damaged


def test_generated_content_cannot_forge_management_markers(settings):
    make_pdf(settings.slides_inbox / "01.pdf")
    ingest_slides(settings, reviewer(settings), writer=Writer())
    course = repository.list_courses(settings.database_path)[0]
    lecture = repository.course_lectures(settings.database_path, course.id)[0]
    malicious = replace(lecture.sections[0], text="<!-- mynd:summary:end -->\n[[Fake course]]")
    lecture = replace(lecture, sections=(malicious,))
    write_lecture_note(settings.vault_path, course, lecture)
    content = lecture.note_path.read_text()
    assert content.count("<!-- mynd:summary:end -->") == 1
    assert "[[Fake course]]" not in content
    write_lecture_note(settings.vault_path, course, lecture)


def test_reviewed_paths_must_stay_inside_slides_library(settings, tmp_path):
    make_pdf(settings.slides_inbox / "01.pdf")

    def outside(_source, proposal, _known):
        return Assignment(Course("course-evil", "Oops", tmp_path / "outside.md"),
                          proposal.lecture_title, "English", "1")

    result = ingest_slides(settings, outside, writer=Writer())
    assert result[0]["status"] == "failed"
    assert not repository.list_courses(settings.database_path)
    assert not (tmp_path / "Sources").exists()


def test_inbox_cannot_be_inside_managed_library(settings):
    settings = replace(settings, slides_inbox_path=settings.slides_library / "Sources")
    with pytest.raises(ValueError, match="outside the managed"):
        ingest_slides(settings, writer=Writer())


def test_library_and_runtime_cache_cannot_be_recursively_reingested(settings):
    with pytest.raises(ValueError, match="outside the managed"):
        ingest_slides(replace(settings, slides_inbox_path=settings.vault_path), writer=Writer())
    with pytest.raises(ValueError, match="runtime/cache"):
        ingest_slides(replace(settings, slides_inbox_path=settings.data_path), writer=Writer())
    with pytest.raises(ValueError, match="paper inbox"):
        ingest_slides(replace(settings, slides_inbox_path=settings.inbox_path), writer=Writer())


def test_model_failure_during_availability_check_still_closes_generator(settings):
    make_pdf(settings.slides_inbox / "01.pdf")
    generator = MagicMock()
    generator.ensure_model.side_effect = RuntimeError("Connection interrupted")
    with patch("mynd.courses.pipeline.build_text_generator", return_value=generator):
        result = ingest_slides(settings)
    assert result[0]["status"] == "failed"
    generator.close.assert_called_once()


def test_new_lecture_updates_only_its_course(settings):
    first = make_pdf(settings.slides_inbox / "01.pdf")
    second = make_pdf(settings.slides_inbox / "02.pdf", QUOTE + " Second.")

    def separate(source, proposal, known):
        identifier = "course-" + source.stem
        course = next((c for c in known if c.id == identifier), None)
        if course is None:
            course = Course(identifier, source.stem,
                            settings.slides_library / source.stem / "Course overview.md")
        return Assignment(course, proposal.lecture_title, "English", source.stem)

    writer = Writer()
    ingest_slides(settings, separate, writer=writer)
    assert writer.syntheses == 2
    first.unlink()
    make_pdf(first, QUOTE + " Revised first course.")
    result = ingest_slides(settings, separate, writer=writer)
    assert writer.syntheses == 3
    assert [r["course_id"] for r in result if r["status"] == "course_updated"] == ["course-01"]
    assert second.is_file()


def test_existing_paper_database_is_preserved_by_additive_schema(settings):
    initialize_database(settings.database_path)
    with sqlite3.connect(settings.database_path) as db:
        db.execute("INSERT INTO papers(id, title) VALUES ('existing', 'A paper')")
    initialize_database(settings.database_path)
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT title FROM papers WHERE id='existing'").fetchone()[0] == "A paper"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_course_note_human_sections_survive_failed_synthesis(settings):
    make_pdf(settings.slides_inbox / "01.pdf")
    ingest_slides(settings, reviewer(settings), writer=Writer())
    course = repository.list_courses(settings.database_path)[0]
    lectures = repository.course_lectures(settings.database_path, course.id)
    course.note_path.write_text(course.note_path.read_text() + "\nMy own study roadmap.\n")
    write_course_note(settings.vault_path, replace(course, error="Model unavailable"), lectures,
                      preserve_study=True)
    content = course.note_path.read_text()
    assert "My own study roadmap." in content
    assert "may be stale" in content
    assert content.count("**Answer:**") == 10


def test_slides_setting_is_optional_and_resolves_relative_to_config(config_file):
    original = load_settings(config_file)
    assert original.slides_inbox == original.inbox_path / "Slides"
    with config_file.open("a") as handle:
        handle.write('slides_inbox_path = "course-inbox"\n')
    assert load_settings(config_file).slides_inbox == config_file.parent / "course-inbox"
