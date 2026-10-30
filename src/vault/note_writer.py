from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from resolution.citations import CitationRecord
from resolution.doi import normalize_doi
from resolution.metadata import PaperMetadata

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


def _slug(text: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|#^\[\]]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:100] or "Untitled Paper"


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _write_atomic(path: Path, content: str) -> None:
    """Write text through a temporary sibling, then rename it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


def _doi_display(doi: str) -> str:
    """Render a DOI as a clickable Markdown link, or a placeholder."""
    return _DOI_LINK.format(doi=doi) if doi else "Not identified"


def _or_unknown(value: object, fallback: str = "Unknown") -> str:
    return str(value) if value else fallback


def write_reference_placeholder(vault: Path, citation: CitationRecord) -> Path:
    """Create a stable note for a cited work that has not been ingested."""
    doi = normalize_doi(citation.get("doi"))
    raw = citation.get("raw_reference", "")
    title = citation.get("title") or raw or "Unknown reference"
    identity = doi.lower() if doi else title.casefold()
    digest = hashlib.sha1(identity.encode("utf-8"), usedforsecurity=False).hexdigest()
    paper_id = "doi-" + digest[:12] if doi else "paper-" + digest[:16]
    placeholders_dir = vault / "11 Citation Placeholders"
    placeholders_dir.mkdir(parents=True, exist_ok=True)
    path = placeholders_dir / f"{_slug(title)} ({paper_id[-12:]}).md"
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
    _write_atomic(path, content)
    return path


def _migrate_bracketed_note_path(path: Path) -> Path:
    """Rename legacy `Title [id].md` notes to parser-safe `Title (id).md` names."""
    match = re.match(r"^(.*) \[([0-9a-fA-F]{12,16})\](\.md)$", path.name)
    if not match or not path.exists():
        return path
    replacement = path.with_name(f"{match.group(1)} ({match.group(2)}){match.group(3)}")
    if replacement.exists():
        return replacement
    path.rename(replacement)
    return replacement


def _obsidian_link(vault: Path, target: str | Path) -> str:
    """Build a vault-relative Obsidian link to an existing note path."""
    path = _migrate_bracketed_note_path(Path(target))
    try:
        link_path = path.resolve().relative_to(vault.resolve()).with_suffix("")
    except ValueError:
        link_path = Path(path.stem)
    return f"[[{link_path.as_posix()}]]"


def link_summary_citations(
    summary: str | None, citations: list[CitationRecord], vault: Path
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
    authors = metadata.get("authors") or []
    fields = (
        ("Authors", _or_unknown(", ".join(authors))),
        ("Year", _or_unknown(metadata.get("year"))),
        ("Venue", _or_unknown(metadata.get("venue"))),
        ("DOI", _doi_display(metadata.get("doi") or "")),
        ("Source PDF", f"`{source_pdf.as_posix()}`"),
        ("Metadata URL", _or_unknown(metadata.get("url"), "Not available")),
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
    slug = _slug(title)
    path = papers_dir / f"{slug} ({paper_id[-12:]}).md"
    legacy = papers_dir / f"{slug} [{paper_id[-12:]}].md"
    if legacy.exists() and not path.exists():
        legacy.rename(path)
    return path


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
    summary_content = summary or _SUMMARY_FALLBACK
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

{metadata.get('abstract') or '_No abstract retrieved._'}

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


def write_paper_note(
    vault: Path,
    paper_id: str,
    metadata: PaperMetadata,
    source_pdf: Path,
    citations: list[CitationRecord] | None = None,
    summary: str | None = None,
) -> Path:
    """Render and atomically write a paper note, preserving human-authored sections."""
    papers_dir = vault / "10 Papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    title = metadata.get("title") or source_pdf.stem
    path = _note_path(papers_dir, title, paper_id)
    human_sections = _read_human_sections(path)
    content = _render_paper_note(
        vault=vault,
        paper_id=paper_id,
        title=title,
        metadata=metadata,
        source_pdf=source_pdf,
        citations=citations or [],
        summary=summary or human_sections.get("Summary"),
        human_sections=human_sections,
    )
    _write_atomic(path, content)
    return path


def write_note(path: Path, content: str) -> None:
    """Atomically write arbitrary Markdown content to a path."""
    _write_atomic(path, content)
