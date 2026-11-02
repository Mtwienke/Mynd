from __future__ import annotations

from pathlib import Path

from mynd.questionnize.note import questionnize_note, read_summary, write_questions
from mynd.settings import Settings
from mynd.storage import repositories

__all__ = ["questionnize_note", "read_summary", "resolve_note_path", "write_questions"]


def resolve_note_path(settings: Settings, target: str) -> Path:
    """Resolve a note path, paper ID, or DOI to an ingested paper's note."""
    candidate = Path(target).expanduser()
    if candidate.is_file():
        return candidate
    if not settings.database_path.is_file():
        raise ValueError(
            f"Not a note path, and no library database exists at {settings.database_path}"
        )

    location = repositories.paper_for_doi(settings.database_path, target)
    if location is None:
        location = repositories.paper_for_id(settings.database_path, target)
    if location is None or not location.note_path:
        raise ValueError(f"No ingested paper found for '{target}'")

    note_path = Path(location.note_path)
    if not note_path.is_file():
        raise ValueError(f"Paper '{target}' points to a missing note: {note_path}")
    return note_path
