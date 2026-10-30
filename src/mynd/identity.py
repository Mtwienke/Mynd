"""Paper work-ID and note-filename rules shared by ingestion and the vault."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_UNSAFE_FILENAME_CHARS = re.compile(r"[\\/:*?\"<>|#^\[\]]")
WHITESPACE = re.compile(r"\s+")
_LEGACY_BRACKETED_NAME = re.compile(r"^(.*) \[([0-9a-fA-F]{12,16})\](\.[mM][dD])$")


def slugify(text: str) -> str:
    """Return a filesystem-safe note title, falling back for empty input."""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("", text)
    cleaned = WHITESPACE.sub(" ", cleaned).strip(" .")
    return cleaned[:100] or "Untitled Paper"


def doi_work_id(doi: str | None) -> str | None:
    """Return the stable work ID for a DOI, or None when no DOI is known."""
    if not doi:
        return None
    digest = hashlib.sha1(doi.lower().encode("utf-8"), usedforsecurity=False)
    return "doi-" + digest.hexdigest()[:12]


def file_work_id(digest: str) -> str:
    """Return the stable work ID for a PDF without a DOI."""
    return "paper-" + digest[:16]


def title_work_id(title: str) -> str:
    """Return a stable work ID for a cited work known only by its title."""
    digest = hashlib.sha1(title.casefold().encode("utf-8"), usedforsecurity=False)
    return "paper-" + digest.hexdigest()[:16]


def note_filename(title: str, work_id: str) -> str:
    """Return the canonical note filename for a title and work ID."""
    return f"{slugify(title)} ({work_id[-12:]}).md"


def migrate_legacy_note(path: Path) -> Path:
    """Rename a legacy ``Title [id].md`` note to the parser-safe parenthesized form."""
    match = _LEGACY_BRACKETED_NAME.match(path.name)
    if not match or not path.exists():
        return path
    replacement = path.with_name(f"{match.group(1)} ({match.group(2)}){match.group(3)}")
    if replacement.exists():
        return replacement
    path.rename(replacement)
    return replacement
