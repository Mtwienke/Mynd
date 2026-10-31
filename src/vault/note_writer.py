from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from mynd.contracts import CitationRecord, PaperMetadata
from mynd.files import atomic_write_text
from mynd.identity import (
    WHITESPACE,
    doi_work_id,
    migrate_legacy_note,
    note_filename,
    title_work_id,
)
from resolution.doi import normalize_doi

_SECTION_HEADINGS = ("Summary", "My notes", "Questions", "Reading log")
_DOI_LINK = "[doi:{doi}](https://doi.org/{doi})"
_SUMMARY_FALLBACK = "_Summary unavailable. Check model availability and retry ingestion._"
_NO_REFERENCES = "_No references extracted yet._"
_DEFAULT_NOTES = (
    "_Add your own interpretation here. This section is preserved during note updates._"
)
_DEFAULT_QUESTIONS = "_What do I want to remember or investigate?_"
_DEFAULT_READING_LOG = "_Add reading dates and observations here._"
_CITATION_GROUP = r"(\d+(?:\s*[,;–-]\s*\d+)*)"


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _doi_display(doi: str) -> str:
    """Render a DOI as a clickable Markdown link, or a placeholder."""
    return _DOI_LINK.format(doi=doi) if doi else "Not identified"


def _or_unknown(value: object, fallback: str = "Unknown") -> str:
    return str(value) if value else fallback


def _sanitize_inline(value: object) -> str:
    """Collapse untrusted text to a single line that cannot start a heading."""
    return WHITESPACE.sub(" ", str(value)).strip().lstrip("#").strip()


def _sanitize_block(value: str) -> str:
    """Neutralize heading markers in untrusted multi-line text."""
    return re.sub(r"(?m)^(\s{0,3})#", r"\1\\#", value)



@dataclass(frozen=True)
class PaperNote:
    """Inputs for rendering and writing one paper note."""

    vault: Path
    paper_id: str
    metadata: PaperMetadata
    source_pdf: Path
    citations: list[CitationRecord] = field(default_factory=list)
    summary: str | None = None


def write_reference_placeholder(vault: Path, citation: CitationRecord) -> Path:
    """Create a stable note for a cited work that has not been ingested."""
    doi = normalize_doi(citation.get("doi"))
    raw = _sanitize_inline(citation.get("raw_reference", ""))
    title = _sanitize_inline(citation.get("title") or raw or "Unknown reference")
    paper_id = doi_work_id(doi) if doi else title_work_id(title)
    placeholders_dir = vault / "11 Citation Placeholders"
    placeholders_dir.mkdir(parents=True, exist_ok=True)
    path = placeholders_dir / note_filename(title, paper_id)
    if path.exists():
        return path
    content = f'''---
type: paper-placeholder
work_id: {_yaml_quote(paper_id)}
doi: {_yaml_quote(doi or "")}
year: null
status: reference-placeholder
---

# {title}

This placeholder was created from a citation. Ingest the paper to replace it with a full paper note.

- **DOI:** {_doi_display(doi or "")}
- **Citation as extracted:** {raw}
'''
    atomic_write_text(path, content)
    return path


def _obsidian_link(vault: Path, target: str | Path) -> str:
    """Build a vault-relative Obsidian link without touching the filesystem."""
    path = Path(target)
    try:
        link_path = path.resolve().relative_to(vault.resolve()).with_suffix("")
    except ValueError:
        link_path = Path(path.stem)
    return f"[[{link_path.as_posix()}]]"


def link_summary_citations(
    vault: Path, summary: str | None, citations: list[CitationRecord]
) -> str | None:
    """Replace numeric bibliography callouts with links in bibliography order."""
    if not summary:
        return summary
    linked = re.sub(
        r"(?<!\[)\[\[" + _CITATION_GROUP + r"\]\](?!\])",
        lambda match: _replace_group(match, citations, vault),
        summary,
    )
    return re.sub(
        r"(?<!\[)\[" + _CITATION_GROUP + r"\](?!\])",
        lambda match: _replace_group(match, citations, vault),
        linked,
    )


def _replace_group(
    match: re.Match[str], citations: list[CitationRecord], vault: Path
) -> str:
    converted = re.sub(
        r"\d+", lambda number: _replace_number(number, citations, vault), match.group(1)
    )
    return f"({converted})"


def _replace_number(
    match: re.Match[str], citations: list[CitationRecord], vault: Path
) -> str:
    index = int(match.group(0)) - 1
    if index < 0 or index >= len(citations):
        return match.group(0)
    citation = citations[index]
    note = citation.get("target_note") or citation.get("placeholder_note")
    return _obsidian_link(vault, note) if note else match.group(0)


def _citation_annotation(citation: CitationRecord) -> str:
    labels = []
    if citation.get("importance"):
        labels.append(f"**{citation['importance']}**")
    if citation.get("direction"):
        labels.append(f"_{citation['direction']}_")
    if citation.get("similarity"):
        labels.append(f"Similarity: {citation['similarity']}")
    return " · ".join(labels)


def _render_citation(vault: Path, citation: CitationRecord) -> str:
    target_path = citation.get("target_note") or citation.get("placeholder_note")
    annotation = _citation_annotation(citation)
    suffix = f" — {annotation}" if annotation else ""
    if target_path:
        return f"- {_obsidian_link(vault, target_path)}{suffix}"
    status = citation.get("status", "unresolved")
    raw = citation.get("raw_reference", "").strip()
    return f"- **{status.title()} reference:** {raw}{suffix}"


def _citation_lines(vault: Path, citations: list[CitationRecord]) -> str:
    if not citations:
        return _NO_REFERENCES
    return "\n".join(_render_citation(vault, citation) for citation in citations)


def _paper_metadata_block(metadata: PaperMetadata, source_pdf: Path) -> str:
    authors = [_sanitize_inline(author) for author in metadata.get("authors") or []]
    fields = (
        ("Authors", _or_unknown(", ".join(authors))),
        ("Year", _or_unknown(metadata.get("year"))),
        ("Venue", _sanitize_inline(_or_unknown(metadata.get("venue")))),
        ("DOI", _doi_display(metadata.get("doi") or "")),
        ("Source PDF", f"`{source_pdf.as_posix()}`"),
        ("Metadata URL", _sanitize_inline(_or_unknown(metadata.get("url"), "Not available"))),
    )
    return "\n".join(f"- **{label}:** {value}" for label, value in fields)


def _read_human_sections(path: Path) -> dict[str, str]:
    """Capture human-authored sections so refreshes never clobber them."""
    if not path.exists():
        return {}
    existing = path.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    for heading in _SECTION_HEADINGS:
        marker = f"## {heading}"
        if marker not in existing:
            continue
        remainder = existing.split(marker, 1)[1]
        next_heading = re.search(r"\n## ", remainder)
        body = remainder[: next_heading.start()] if next_heading else remainder
        sections[heading] = body.strip()
    return sections


def _note_path(papers_dir: Path, title: str, paper_id: str) -> Path:
    """Return the current note path, migrating legacy bracketed filenames."""
    return migrate_legacy_note(papers_dir / note_filename(title, paper_id))


def _render_paper_note(
    *,
    vault: Path,
    paper_id: str,
    title: str,
    metadata: PaperMetadata,
    source_pdf: Path,
    citations: list[CitationRecord],
    summary: str | None,
    human_sections: dict[str, str],
) -> str:
    summary_content = _sanitize_block(summary or _SUMMARY_FALLBACK)
    abstract = _sanitize_block(metadata.get('abstract') or '_No abstract retrieved._')
    return f"""---
type: paper
work_id: {_yaml_quote(paper_id)}
doi: {_yaml_quote(metadata.get("doi") or "")}
year: {metadata.get("year") or 'null'}
status: inbox
---

# {title}

{_paper_metadata_block(metadata, source_pdf)}

## Summary

{summary_content}

## Abstract

{abstract}

## My notes

{human_sections.get('My notes', _DEFAULT_NOTES)}

## References

Reference labels are tentative reading aids inferred from this paper's text.

{_citation_lines(vault, citations)}

## Questions

{human_sections.get('Questions', _DEFAULT_QUESTIONS)}

## Reading log

{human_sections.get('Reading log', _DEFAULT_READING_LOG)}

## Provenance

- Ingested by Mynd.
- Bibliographic metadata may be incomplete; verify against the paper.
"""


def write_paper_note(note: PaperNote) -> Path:
    """Render and atomically write a paper note, preserving human-authored sections."""
    papers_dir = note.vault / "10 Papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    title = _sanitize_inline(note.metadata.get("title") or note.source_pdf.stem)
    path = _note_path(papers_dir, title, note.paper_id)
    human_sections = _read_human_sections(path)
    content = _render_paper_note(
        vault=note.vault,
        paper_id=note.paper_id,
        title=title,
        metadata=note.metadata,
        source_pdf=note.source_pdf,
        citations=note.citations,
        summary=note.summary or human_sections.get("Summary"),
        human_sections=human_sections,
    )
    atomic_write_text(path, content)
    return path
