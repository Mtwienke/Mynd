"""Canonical, non-AI publication-year hubs shared by ingestion and migration."""
from __future__ import annotations

from pathlib import Path

from mynd.resolution.publication_year import valid_year
from mynd.vault.markdown import parse_document

YEARS_FOLDER = Path("Years")


def decade_folder(year: int) -> str:
    if valid_year(year) is None:
        raise ValueError(f"Invalid publication year: {year}")
    decade = year // 10 * 10
    # A fixed, century-safe window, not a moving pivot that relocates old notes.
    return f"{decade % 100:02d}s" if 1950 <= decade <= 2040 else f"{decade}s"


def year_path(year: int) -> Path:
    return YEARS_FOLDER / decade_folder(year) / f"year-{year}.md"


def year_link(value: object) -> str:
    year = valid_year(value)
    return f"[[{year_path(year).with_suffix('').as_posix()}|{year}]]" if year else "Unknown"


def render_year_note(year: int) -> str:
    year_path(year)
    return (f'---\ntype: year\nwork_id: "year-{year}"\nyear: {year}\n'
            f'decade: {year // 10 * 10}\naliases: ["{year}"]\ntags: [mynd/year]\n---\n\n'
            f"# {year}\n\nPublication-year hub. Papers and citation placeholders link here "
            "using their recorded publication year, not their ingestion date.\n\n"
            "Use Obsidian's backlinks or local graph to see the associated publications.\n\n"
            "## My notes\n\n_Add chronological context here; this note is never regenerated._\n")


def validate_year_note(path: Path, vault: Path, year: int) -> None:
    doc = parse_document(path, vault)
    if doc.errors:
        raise ValueError(f"Year note has syntax/ownership errors: {path}")
    if doc.kind in {"paper", "paper-placeholder", "lecture", "course", "topic"}:
        raise ValueError(f"Year-note path is occupied by a {doc.kind}: {path}")
    if doc.metadata.get("year") is not None and valid_year(doc.metadata["year"]) != year:
        raise ValueError(f"Year-note metadata conflicts with {year}: {path}")


def ensure_year_note(vault: Path, value: object) -> Path | None:
    year = valid_year(value)
    if year is None:
        return None
    path = vault / year_path(year)
    if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(vault)):
        raise ValueError(f"Refusing a symlinked year-note path: {path}")
    if path.exists():
        validate_year_note(path, vault, year)
    else:
        # Exclusive creation never overwrites an existing or concurrently created hub.
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="") as handle:
                handle.write(render_year_note(year))
        except FileExistsError:
            validate_year_note(path, vault, year)
    return path
