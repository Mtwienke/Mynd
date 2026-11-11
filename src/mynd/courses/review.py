"""Editable review drafts at the CLI boundary; nothing is saved by this module."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

import typer
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, HSplit, Layout, ScrollablePane, VSplit
from prompt_toolkit.layout.containers import DynamicContainer
from prompt_toolkit.output import create_output
from prompt_toolkit.widgets import Box, Button, Dialog, Frame, Label, RadioList, TextArea

from mynd.courses.models import Assignment, Course, CourseProposal
from mynd.identity import slugify

FIELDS = (
    ("course_title", "Course name"), ("university", "University"),
    ("semester", "Semester"), ("lecturer", "Lecturer"),
    ("course_language", "Course output language"), ("title", "Lecture title"),
    ("language", "Lecture language"), ("order", "Lecture order"),
)
SHARED_FIELDS = ("course_title", "university", "semester", "lecturer", "course_language")


@dataclass
class ReviewDraft:
    proposal: CourseProposal
    known: list[Course]
    library: Path
    source: Path
    course: Course | None = None
    values: dict[str, str] = field(default_factory=dict)
    new_id: str = field(default_factory=lambda: "course-" + uuid4().hex[:12])

    def __post_init__(self):
        self.values.update(title=self.proposal.lecture_title, language=self.proposal.language,
                           order=self.proposal.order if self.proposal.saved
                           else self.proposal.order or self.source.stem)
        self.select(next((c for c in self.known if c.id == self.proposal.course_id), None))

    def select(self, course: Course | None) -> None:
        self.course = course
        self.values.update(
            course_title=course.title if course else self.proposal.course_title,
            university=course.university if course else self.proposal.university,
            semester=course.semester if course else self.proposal.semester,
            lecturer=course.lecturer if course else self.proposal.lecturer,
            course_language=course.language if course else self.proposal.language,
        )

    def choose(self, value: str) -> None:
        value = value.strip()
        if value.casefold() == "new":
            self.select(None)
            return
        if value.lstrip("+-").isdecimal():
            index = int(value)
            if 1 <= index <= len(self.known):
                self.select(self.known[index - 1])
                return
            raise ValueError("Invalid course number. Choose a listed number or enter a name.")
        if not value:
            raise ValueError("The course name must not be empty.")
        normalized = " ".join(value.split()).casefold()
        matches = [c for c in self.known if " ".join(c.title.split()).casefold() == normalized]
        if len(matches) > 1:
            raise ValueError("Several courses have that name. Choose a listed number.")
        self.select(matches[0] if matches else None)
        if not matches:
            self.values["course_title"] = value

    def assignment(self) -> Assignment:
        values = {key: value.strip() for key, value in self.values.items()}
        if not values["course_title"] or not values["title"]:
            raise ValueError("Course name and lecture title must not be empty.")
        metadata = {"title": values["course_title"], "university": values["university"],
                    "semester": values["semester"], "lecturer": values["lecturer"],
                    "language": values["course_language"]}
        if self.course:
            course = replace(self.course, **metadata)
        else:
            course = Course(self.new_id, note_path=self.library / (
                f"{slugify(values['course_title'])} ({self.new_id[-12:]})"
            ) / "Course overview.md", **metadata)
        return Assignment(course, values["title"], values["language"], values["order"])


def _confirmation(draft: ReviewDraft) -> str:
    values = "\n".join(f"{label}: {draft.values[key] or '(unknown)'}" for key, label in FIELDS)
    scope = (f"Editing shared course {draft.course.id}: course fields apply to ALL its lectures."
             if draft.course else "Create a new course.")
    return f"{values}\n\n{scope}\nStable note paths and personal notes are preserved."


def _evidence(proposal: CourseProposal) -> str:
    return "\n\n".join(f"Page {s.page}: {s.quote}" for s in proposal.evidence) or "No source excerpts."


class FormLayout(Layout):
    # Include off-screen fields: focusing them lets ScrollablePane scroll them into view.
    # Default Layout navigation only includes windows visible at the last render.
    def _move(self, offset: int) -> None:
        windows = list(self.get_focusable_windows())
        if windows:
            index = windows.index(self.current_window) if self.current_window in windows else 0
            self.focus(windows[(index + offset) % len(windows)])

    def focus_next(self) -> None:
        self._move(1)

    def focus_previous(self) -> None:
        self._move(-1)


class ReviewForm:
    """A scrollable, keyboard-accessible form, with a separate final confirmation."""

    def __init__(self, draft: ReviewDraft, *, input=None, output=None):
        self.draft = draft
        output = output or create_output(stdout=sys.stderr)
        self.entries = {key: TextArea(text=draft.values[key], multiline=False, height=1,
                                      wrap_lines=False) for key, _ in FIELDS}
        self.message = Label("Course fields are shared. Blank optional fields mean unknown.")
        self.course_button = Button("Change course…", handler=self.choose_course)
        rows = HSplit([
            Label("Tab / Shift-Tab: move · Ctrl-S: save · Ctrl-C: cancel"),
            Label(lambda: f"Existing course [{draft.course.id}] — shared metadata"
                  if draft.course else "New course — not saved yet"),
            self.course_button,
            Label("Rename using Course name; Change course switches membership."),
            *[VSplit([Label(label + ":", width=24), self.entries[key]]) for key, label in FIELDS],
            self.message,
        ])
        evidence = Frame(TextArea(text=_evidence(draft.proposal), read_only=True,
                                  scrollbar=True, height=6), title="Slide evidence (read-only)")
        # One column on small terminals; side-by-side evidence on wider screens.
        wide = VSplit([Box(rows, padding=0), evidence], padding=2)
        narrow = HSplit([rows, evidence], padding=1)
        body = ScrollablePane(DynamicContainer(
            lambda: wide if output.get_size().columns >= 110 else narrow,
        ))
        self.dialog = Dialog(
            title=f"Review slide assignment: {draft.source.name}", body=body,
            buttons=[Button("Save", handler=self.save), Button("Skip file", handler=self.skip),
                     Button("Cancel run", handler=self.cancel)], width=Dimension(preferred=120),
            with_background=True,
        )
        bindings = KeyBindings()
        bindings.add("c-s")(lambda _: self.save())
        bindings.add("c-c")(lambda _: self.cancel())
        self.app = Application(
            layout=FormLayout(self.dialog, focused_element=self.entries["course_title"]),
            key_bindings=bindings, full_screen=True, mouse_support=True,
            input=input, output=output,
        )

    def sync(self):
        self.draft.values.update({key: entry.text for key, entry in self.entries.items()})

    def restore(self):
        self.app.layout = FormLayout(self.dialog, focused_element=self.entries["course_title"])
        self.app.layout.update_parents_relations()

    def choose_course(self):
        self.sync()
        choices = RadioList([("new", "New course")] + [
            (c.id, f"{c.title} / {c.university or '?'} / {c.semester or '?'} [{c.id}]")
            for c in self.draft.known
        ], default=self.draft.course.id if self.draft.course else "new")

        def use():
            self.draft.select(next((c for c in self.draft.known
                                    if c.id == choices.current_value), None))
            for key in SHARED_FIELDS:
                self.entries[key].text = self.draft.values[key]
            self.restore()

        dialog = Dialog(title="Choose membership (Space selects, Tab moves to Use)",
                        body=Box(choices, height=8), buttons=[Button("Use", handler=use),
                        Button("Back", handler=self.restore)], with_background=True)
        self.app.layout = FormLayout(dialog, focused_element=choices)
        self.app.layout.update_parents_relations()

    def save(self):
        self.sync()
        try:
            assignment = self.draft.assignment()
        except ValueError as exc:
            self.message.text = str(exc)
            self.restore()
            return
        confirm = Button("Confirm", handler=lambda: self.app.exit(result=assignment))
        confirmation = TextArea(text=_confirmation(self.draft), read_only=True, scrollbar=True,
                                height=Dimension(min=3, preferred=13, max=18))
        dialog = Dialog(title="Save this assignment?", body=confirmation,
                        buttons=[confirm, Button("Edit again", handler=self.restore)],
                        with_background=True)
        self.app.layout = FormLayout(dialog, focused_element=confirm)
        self.app.layout.update_parents_relations()

    def skip(self):
        self.app.exit(result=None)

    def cancel(self):
        self.app.exit(exception=KeyboardInterrupt())

    def run(self) -> Assignment | None:
        return self.app.run()


def _plain_review(draft: ReviewDraft) -> Assignment | None:
    """Same draft/confirmation semantics for dumb terminals and CLI test runners."""
    typer.echo(_evidence(draft.proposal), err=True)
    while True:
        typer.echo(f"\nReview slide assignment: {draft.source.name}", err=True)
        typer.echo("Existing course (shared fields affect all its lectures): " + (
            draft.course.id if draft.course else "new"), err=True)
        for index, (key, label) in enumerate(FIELDS, 1):
            typer.echo(f"  {index}. {label}: {draft.values[key] or '(unknown)'}", err=True)
        action = typer.prompt("Field number, course, save, skip, or cancel", default="save",
                              err=True).strip().casefold()
        if action == "cancel":
            raise KeyboardInterrupt
        if action == "skip":
            return None
        try:
            if action == "course":
                for index, course in enumerate(draft.known, 1):
                    typer.echo(f"  {index}: {course.title} / {course.university or '?'} / "
                               f"{course.semester or '?'} [{course.id}]", err=True)
                draft.choose(typer.prompt("Course number or name, or 'new'", err=True))
            elif action.isdecimal() and 1 <= int(action) <= len(FIELDS):
                key, label = FIELDS[int(action) - 1]
                draft.values[key] = typer.prompt(label, default="", show_default=False, err=True)
            elif action == "save":
                assignment = draft.assignment()
                typer.echo(_confirmation(draft), err=True)
                if typer.confirm("Save this assignment? (No returns to editing)", default=True,
                                 err=True):
                    return assignment
            else:
                typer.echo("Choose a field number, course, save, skip, or cancel.", err=True)
        except ValueError as exc:
            typer.echo(str(exc), err=True)


def review_assignment(source: Path, proposal: CourseProposal, known: list[Course],
                      library: Path, *, plain: bool = False) -> Assignment | None:
    draft = ReviewDraft(proposal, known, library, source)
    if plain or not sys.stdin.isatty() or not sys.stderr.isatty():
        return _plain_review(draft)
    return ReviewForm(draft).run()
