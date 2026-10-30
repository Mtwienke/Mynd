from __future__ import annotations

import hashlib
import re
from pathlib import Path

from resolution.citations import CitationRecord
from resolution.doi import normalize_doi
from resolution.metadata import PaperMetadata


def _slug(text: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|#^\[\]]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:100] or "Untitled Paper"


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


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

- **DOI:** {f'[doi:{doi}](https://doi.org/{doi})' if doi else 'Not identified'}
- **Citation as extracted:** {raw}
'''
    write_note(path, content)
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

    def replace_group(match: re.Match[str]) -> str:
        group = match.group(1)

        def replace_number(number_match: re.Match[str]) -> str:
            index = int(number_match.group(0)) - 1
            if index < 0 or index >= len(citations):
                return number_match.group(0)
            citation = citations[index]
            note = citation.get("target_note") or citation.get("placeholder_note")
            return _obsidian_link(vault, note) if note else number_match.group(0)

        converted = re.sub(r"\d+", replace_number, group)
        return f"({converted})"

    citation_group = r"(\d+(?:\s*[,;–-]\s*\d+)*)"
    summary = re.sub(r"(?<!\[)\[\[" + citation_group + r"\]\](?!\])", replace_group, summary)
    return re.sub(r"(?<!\[)\[" + citation_group + r"\](?!\])", replace_group, summary)


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
    path = papers_dir / f"{_slug(title)} ({paper_id[-12:]}).md"
    legacy_path = papers_dir / f"{_slug(title)} [{paper_id[-12:]}].md"
    if legacy_path.exists() and not path.exists():
        legacy_path.rename(path)

    authors = metadata.get("authors") or []
    doi = metadata.get("doi") or ""
    year = metadata.get("year") or ""
    venue = metadata.get("venue") or ""
    citation_lines = []
    for citation in citations or []:
        target_path = citation.get("target_note") or citation.get("placeholder_note")
        raw = citation.get("raw_reference", "").strip()
        labels = []
        if citation.get("importance"):
            labels.append(f"**{citation['importance']}**")
        if citation.get("direction"):
            labels.append(f"_{citation['direction']}_")
        if citation.get("similarity"):
            labels.append(f"Similarity: {citation['similarity']}")
        annotation = " · ".join(labels)
        if target_path:
            citation_lines.append(
                f"- {_obsidian_link(vault, target_path)}"
                + (f" — {annotation}" if annotation else "")
            )
        else:
            status = citation.get("status", "unresolved")
            citation_lines.append(
                f"- **{status.title()} reference:** {raw}"
                + (f" — {annotation}" if annotation else "")
            )
    cites = "\n".join(citation_lines) if citation_lines else "_No references extracted yet._"

    human_sections: dict[str, str] = {}
    if path.exists():
        old = path.read_text(encoding="utf-8")
        for heading in ("Summary", "My notes", "Questions", "Reading log"):
            marker = f"## {heading}"
            if marker in old:
                section = old.split(marker, 1)[1]
                next_heading = re.search(r"\n## ", section)
                human_sections[heading] = (
                    section[: next_heading.start()].strip() if next_heading else section.strip()
                )

    summary_content = summary or human_sections.get("Summary")
    if not summary_content:
        summary_content = "_Summary unavailable. Check model availability and retry ingestion._"

    content = f"""---
type: paper
work_id: {_yaml_quote(paper_id)}
doi: {_yaml_quote(doi)}
year: {year if year else 'null'}
status: inbox
---

# {title}

- **Authors:** {', '.join(authors) if authors else 'Unknown'}
- **Year:** {year or 'Unknown'}
- **Venue:** {venue or 'Unknown'}
- **DOI:** {f'[doi:{doi}](https://doi.org/{doi})' if doi else 'Not identified'}
- **Source PDF:** `{source_pdf.as_posix()}`
- **Metadata URL:** {metadata.get('url') or 'Not available'}

## Summary

{summary_content}

## Abstract

{metadata.get('abstract') or '_No abstract retrieved._'}

## My notes

{human_sections.get('My notes', '_Add your own interpretation here. This section is preserved during note updates._')}

## References

Reference labels are tentative reading aids inferred from this paper's text.

{cites}

## Questions

{human_sections.get('Questions', '_What do I want to remember or investigate?')}

## Reading log

{human_sections.get('Reading log', '_Add reading dates and observations here._')}

## Provenance

- Ingested by Mynd.
- Bibliographic metadata may be incomplete; verify against the paper.
"""
    temp = path.with_suffix(".md.tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)
    return path


def write_note(path: Path, content: str) -> None:
    """Atomically write arbitrary Markdown content to a path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)
