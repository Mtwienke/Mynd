"""Course notes with explicit managed blocks; everything outside them is preserved."""
from __future__ import annotations

import json
import re
from pathlib import Path

from mynd.courses.models import Course, Lecture, StudyQuestion, StudySection
from mynd.files import atomic_write_text


def _inline(value: str) -> str:
    return re.sub(r"[\r\n\[\]|#]", " ", value).strip()


def _body(value: str) -> str:
    # Model/source content cannot forge management markers or graph links.
    value = value.replace("<!--", "&lt;!--").replace("[[", "\\[\\[")
    return re.sub(r"(?m)^(\s{0,3})#{1,6}\s", r"\1", value).strip()


def note_link(vault: Path, target: Path, label: str = "") -> str:
    relative = target.resolve().relative_to(vault.resolve())
    if relative.suffix == ".md":
        relative = relative.with_suffix("")
    return f"[[{relative.as_posix()}|{_inline(label or target.stem)}]]"


def _sources(vault: Path, sources: tuple, lectures: dict[str, Lecture]) -> str:
    links = []
    for source in sources:
        lecture = lectures[source.lecture_id]
        path = lecture.pdf_path.resolve().relative_to(vault.resolve()).as_posix()
        label = f"{_inline(lecture.title)}, p. {source.page}"
        link = f"[[{path}#page={source.page}|{label}]]"
        if link not in links:
            links.append(link)
    return ", ".join(links)


def _sections(vault: Path, sections: tuple[StudySection, ...], lectures: dict) -> str:
    if not sections:
        return "_Study material unavailable. Check local model availability and retry._"
    return "\n\n".join(
        f"### {_inline(s.heading)}\n\n{_body(s.text)}\n\n"
        f"**Sources:** {_sources(vault, s.sources, lectures)}" for s in sections
    )


def _questions(vault: Path, questions: tuple[StudyQuestion, ...], lectures: dict) -> str:
    if not questions:
        return "_Questions unavailable: insufficient supported evidence or generation failed._"
    result = []
    for group, label in (("concepts", "Concepts and definitions"),
                         ("application", "Understanding and application"),
                         ("synthesis", "Connections and synthesis")):
        items = [q for q in questions if q.group == group]
        result.append(f"### {label}\n\n" + "\n\n".join(
            f"{index}. {_body(q.question)}\n\n"
            f"   **Answer:** {_body(q.answer)}\n\n"
            f"   **Sources:** {_sources(vault, q.sources, lectures)}"
            for index, q in enumerate(items, start=1)
        ))
    return "\n\n".join(result)


def _write(path: Path, kind: str, identifier: str, title: str, blocks: dict[str, str], *,
           previous_title: str | None = None) -> Path:
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    prefix = f"---\ntype: {kind}\nwork_id: {json.dumps(identifier)}\n---\n\n"
    if not content:
        content = prefix + f"# {_inline(title)}\n\n"
        for key, body in blocks.items():
            content += f"<!-- mynd:{key}:begin -->\n{body}\n<!-- mynd:{key}:end -->\n\n"
        content += "## My notes\n\n_Add personal notes here; this area is never regenerated._\n"
    else:
        # Only rename our exact untouched generated heading. Customized headings,
        # frontmatter, personal notes, and all other unmanaged content stay intact.
        if previous_title is not None:
            old_header = prefix + f"# {_inline(previous_title)}\n\n"
            if content.startswith(old_header):
                content = prefix + f"# {_inline(title)}\n\n" + content[len(old_header):]
        for key, body in blocks.items():
            begin, end = f"<!-- mynd:{key}:begin -->", f"<!-- mynd:{key}:end -->"
            if content.count(begin) != 1 or content.count(end) != 1:
                raise ValueError(f"Managed block '{key}' is missing/duplicated in {path}; "
                                 "restore its markers before regenerating")
            pattern = re.compile(r"(?m)^" + re.escape(begin) + r"\n.*?^" + re.escape(end),
                                 re.DOTALL)
            replacement = f"{begin}\n{body}\n{end}"
            content, count = pattern.subn(lambda _, value=replacement: value, content)
            if count != 1:
                raise ValueError(f"Damaged managed block '{key}' in {path}")
    atomic_write_text(path, content)
    return path


def write_lecture_note(vault: Path, course: Course, lecture: Lecture, *,
                       preserve_study: bool = False, previous_title: str | None = None) -> Path:
    lookup = {lecture.id: lecture}
    metadata = (f"**Lecture:** {_inline(lecture.title)}\n\n"
                f"**Course:** {note_link(vault, course.note_path, course.title)}\n\n"
                f"**Source PDF:** {note_link(vault, lecture.pdf_path)}\n\n"
                f"**Original:** {note_link(vault, lecture.original_path)}\n\n"
                f"**Language:** {_inline(lecture.language) or 'Unknown'} · "
                f"**Order:** {_inline(lecture.order)} · **Status:** {lecture.status}")
    warnings = [
        ("Text-first extraction only: diagrams, screenshots, mathematical layout and "
         "vector graphics may be incomplete even on pages without an automatic warning."),
    ]
    for page in lecture.pages:
        if page.warnings:
            path = lecture.pdf_path.resolve().relative_to(vault.resolve()).as_posix()
            warnings.append(f"[[{path}#page={page.number}|Page {page.number}]]: "
                            + " ".join(page.warnings))
    if lecture.error:
        warnings.append("Generation issue: " + _body(lecture.error))
    if preserve_study and lecture.note_path.exists():
        warnings.append("The previous summary and questions may be stale; regeneration is pending.")
    blocks = {
        "metadata": metadata,
        "quality": "## Extraction quality and provenance\n\n" + "\n".join(
            "- " + warning for warning in warnings
        ) + "\n\nGenerated locally by Mynd. Verify mathematical claims against the source slides.",
    }
    if not preserve_study or not lecture.note_path.exists():
        blocks["summary"] = "## Summary\n\n" + _sections(vault, lecture.sections, lookup)
        blocks["questions"] = "## Extracted questions\n\n" + _questions(
            vault, lecture.questions, lookup)
        blocks = {key: blocks[key] for key in ("metadata", "summary", "questions", "quality")}
    return _write(lecture.note_path, "lecture", lecture.id, lecture.title, blocks,
                  previous_title=previous_title)


def write_course_note(vault: Path, course: Course, lectures: list[Lecture], *,
                      preserve_study: bool = False, previous_title: str | None = None) -> Path:
    lookup = {lecture.id: lecture for lecture in lectures}
    ready = sum(bool(lecture.sections) for lecture in lectures)
    languages = sorted({lecture.language for lecture in lectures if lecture.language})
    metadata = "\n".join(f"- **{label}:** {_inline(value) or 'Unknown'}" for label, value in (
        ("Course", course.title),
        ("University", course.university), ("Semester", course.semester),
        ("Lecturer", course.lecturer), ("Output language", course.language),
    ))
    index = "\n".join(
        f"- {note_link(vault, lecture.note_path, lecture.title)} "
        f"— {_inline(lecture.order)} ({lecture.status})" for lecture in lectures
    )
    coverage = (f"Based on {ready} of {len(lectures)} ingested lecture files with usable study "
                "material—not necessarily the entire course syllabus. Text-only extraction "
                "may omit important visual content.")
    if len(languages) > 1:
        coverage += " Mixed source languages: " + ", ".join(map(_inline, languages)) + "."
    if len(lectures) < 2:
        coverage += " Only one lecture is available; the quiz is not yet cross-lecture."
    if course.error:
        coverage += "\n\nGeneration issue: " + _body(course.error)
    if preserve_study and course.note_path.exists():
        coverage += "\n\nThe previous synthesis and quiz below may be stale; regeneration is pending."
    blocks = {
        "metadata": metadata,
        "coverage": "## Coverage\n\n" + coverage,
        "lectures": "## Lectures\n\n" + index,
    }
    if not preserve_study or not course.note_path.exists():
        blocks["summary"] = "## Course synthesis and study map\n\n" + _sections(
            vault, () if preserve_study else course.sections, lookup)
        blocks["questions"] = "## Course revision quiz\n\n" + _questions(
            vault, () if preserve_study else course.questions, lookup)
        blocks = {key: blocks[key] for key in (
            "metadata", "coverage", "summary", "lectures", "questions",
        )}
    return _write(course.note_path, "course", course.id, course.title, blocks,
                  previous_title=previous_title)
