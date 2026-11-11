"""Review saved metadata without resetting identities, archives, or personal notes."""
from __future__ import annotations

from dataclasses import replace

from mynd.courses.models import Assignment, Course, CourseProposal, Evidence, Lecture
from mynd.settings import Settings
from mynd.storage import courses as repository
from mynd.vault.course_notes import write_course_note, write_lecture_note


def course_metadata(course: Course) -> tuple[str, ...]:
    return (course.title, course.university, course.semester, course.lecturer, course.language)


def saved_proposal(lecture: Lecture, course: Course) -> CourseProposal:
    # Saved human choices, not the original model guess, are the editing defaults.
    evidence = tuple(Evidence(lecture.id, page.number, page.text[:400])
                     for page in lecture.pages[:3] if page.text.strip())
    return CourseProposal(lecture.title, course.title, course.id, course.university,
                          course.semester, course.lecturer, lecture.language, lecture.order, evidence,
                          saved=True)


def validate_assignment(settings: Settings, assignment: Assignment) -> None:
    if not assignment.title.strip() or not assignment.course.title.strip():
        raise ValueError("Confirmed course and lecture titles must not be empty")
    assignment.course.note_path.resolve().relative_to(settings.slides_library.resolve())
    existing = next((c for c in repository.list_courses(settings.database_path)
                     if c.id == assignment.course.id), None)
    if existing and existing.note_path != assignment.course.note_path:
        raise ValueError("Editing metadata must not change an existing course's stable note path")


def save_reviewed_course(settings: Settings, course: Course) -> Course:
    """A newly assigned lecture may also correct fields on an existing shared course."""
    previous = next((c for c in repository.list_courses(settings.database_path)
                     if c.id == course.id), None)
    changed = previous is not None and course_metadata(previous) != course_metadata(course)
    if changed:
        course = replace(course, sections=(), questions=(), fingerprint="",
                         error="Metadata changed; study generation is pending")
    repository.save_course(settings.database_path, course)
    if changed:
        members = repository.course_lectures(settings.database_path, course.id)
        for member in members:
            write_lecture_note(settings.vault_path, course, member)
        write_course_note(settings.vault_path, course, members, previous_title=previous.title)
    return course


def apply_assignment(settings: Settings, lecture: Lecture, assignment: Assignment) -> Lecture:
    """Persist an approved correction; invalidate dependent content, never move files."""
    validate_assignment(settings, assignment)
    known = {c.id: c for c in repository.list_courses(settings.database_path)}
    old_course = known[lecture.course_id]
    course = assignment.course
    previous_course = known.get(course.id)
    course_changed = (previous_course is None
                      or course_metadata(previous_course) != course_metadata(course))
    changed = (lecture.course_id, lecture.title, lecture.language, lecture.order) != (
        course.id, assignment.title, assignment.language, assignment.order,
    )
    if not changed and not course_changed:
        return lecture
    content_changed = (lecture.course_id, lecture.title, lecture.language) != (
        course.id, assignment.title, assignment.language,
    )
    updated = replace(lecture, course_id=course.id, title=assignment.title,
                      language=assignment.language, order=assignment.order)
    if content_changed:
        updated = replace(updated, sections=(), questions=(), status="pending", error="")

    affected = {old_course.id: old_course, course.id: course}
    for identifier, item in affected.items():
        affected[identifier] = replace(item, sections=(), questions=(), fingerprint="",
                                       error="Metadata changed; study generation is pending")
        repository.save_course(settings.database_path, affected[identifier])
    repository.save_lecture(settings.database_path, updated)
    # Refresh every shared course label, including lectures no longer in the inbox.
    for item in affected.values():
        lectures = repository.course_lectures(settings.database_path, item.id)
        for member in lectures:
            write_lecture_note(settings.vault_path, item, member,
                               previous_title=lecture.title if member.id == lecture.id else None)
        write_course_note(settings.vault_path, item, lectures,
                          previous_title=known[item.id].title if item.id in known else None)
    return updated
