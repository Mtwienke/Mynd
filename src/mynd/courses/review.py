"""Interactive confirmation belongs at the CLI boundary, never in model inference."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import typer

from mynd.courses.models import Assignment, Course, CourseProposal
from mynd.identity import slugify


def review_assignment(source: Path, proposal: CourseProposal, known: list[Course],
                      library: Path) -> Assignment | None:
    typer.echo(f"\nReview slide assignment: {source.name}", err=True)
    typer.echo(f"Lecture: {proposal.lecture_title}\n"
               f"Proposed course: {proposal.course_title or 'Unknown—please supply a name'}\n"
               f"University: {proposal.university or 'Unknown'}; "
               f"semester: {proposal.semester or 'Unknown'}; "
               f"lecturer: {proposal.lecturer or 'Unknown'}", err=True)
    for evidence in proposal.evidence:
        typer.echo(f"  Page {evidence.page}: {evidence.quote}", err=True)
    order = proposal.order or source.stem
    typer.echo(f"Proposed order: {order} "
               f"({'slide evidence' if proposal.order else 'filename fallback only'})", err=True)
    if not typer.confirm("Review and assign this lecture?", default=True, err=True):
        return None
    for index, course in enumerate(known, start=1):
        typer.echo(f"  {index}: {course.title} / {course.university or '?'} / "
                   f"{course.semester or '?'} [{course.id}]", err=True)
    default = next((str(index) for index, c in enumerate(known, start=1)
                    if c.id == proposal.course_id), "new")
    choice = typer.prompt("Course number, 'new', or 'skip'", default=default, err=True)
    if choice.lower() == "skip":
        return None
    if choice.lower() == "new":
        title = typer.prompt("Course name", default=proposal.course_title or None, err=True).strip()
        if not title:
            raise ValueError("A confirmed course name is required")
        university = typer.prompt("University (unknown if absent)",
                                  default=proposal.university, err=True)
        semester = typer.prompt("Semester (unknown if absent)", default=proposal.semester, err=True)
        lecturer = typer.prompt("Lecturer (unknown if absent)", default=proposal.lecturer, err=True)
        identifier = "course-" + uuid4().hex[:12]
        course = Course(identifier, title,
                        library / f"{slugify(title)} ({identifier[-12:]})" / "Course overview.md",
                        university, semester, lecturer, proposal.language)
    else:
        try:
            index = int(choice)
            if index < 1:
                raise ValueError
            course = known[index - 1]
        except (ValueError, IndexError) as exc:
            raise ValueError("Invalid course selection; no assignment was saved") from exc
    title = typer.prompt("Lecture title", default=proposal.lecture_title, err=True).strip()
    language = typer.prompt("Source language", default=proposal.language or "Unknown", err=True)
    order = typer.prompt("Lecture order", default=order, err=True)
    typer.echo(f"Confirm: {title} → {course.title}; order={order}; language={language}", err=True)
    if not typer.confirm("Save this assignment?", default=True, err=True):
        return None
    return Assignment(course, title, language, order)
