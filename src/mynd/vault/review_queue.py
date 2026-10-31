from __future__ import annotations

from pathlib import Path

from mynd.files import atomic_write_text
from mynd.settings import Settings
from mynd.storage.database import initialize_database
from mynd.storage.repositories import unresolved_citations


def write_review_queue(settings: Settings) -> Path:
    """Write unresolved/ambiguous citation records to a human-review note."""
    initialize_database(settings.database_path)
    output = settings.vault_path / "90 System" / "Citation Review Queue.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = unresolved_citations(settings.database_path)
    lines = [
        "# Citation Review Queue",
        "",
        "Review these references and link them to notes when appropriate.",
        "",
    ]
    for title, raw, doi, status in rows:
        lines.append(f"- **{status}** — from *{title}*: {raw}")
        if doi:
            lines.append(f"  - DOI: https://doi.org/{doi}")
    if not rows:
        lines.append("_No pending citation records._")
    atomic_write_text(output, "\n".join(lines) + "\n")
    return output
