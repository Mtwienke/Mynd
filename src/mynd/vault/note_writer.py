from __future__ import annotations

import json
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
from mynd.resolution.doi import normalize_doi

_SECTION_HEADINGS = ("Summary", "My notes", "Questions", "Reading log")
_DOI_LINK = "[doi:{doi}](https://doi.org/{doi})"
_SUMMARY_FALLBACK = "_Summary unavailable. Check model availability and retry ingestion._"
_NO_REFERENCES = "_No references extracted yet._"
_DEFAULT_NOTES = (
    "_Add your own interpretation here. This section is preserved during note updates._"
)
_DEFAULT_QUESTIONS = "_What do I want to remember or investigate?_"
_DEFAULT_READING_LOG = "_Add reading dates and observations here._"
_DEFAULT_PLACEHOLDER_NOTES = "_Add your own notes about this reference here._"
_CITATION_GROUP = r"(\d+(?:\s*[,;–-]\s*\d+)*)"
_PARENTHESIZED_YEAR = re.compile(r"\(\s*(\d{4})\s*\)")
_LOOSE_YEAR = re.compile(r"\b(\d{4})\b")
_CITED_BY_LINE = re.compile(r"(?m)^- \*\*Cited by:\*\* (.+)$")
_OBSIDIAN_LINK = re.compile(r"\[\[[^\]]+\]\]")
_PLACEHOLDER_FIELDS = ("venue", "volume", "issue", "pages", "publisher", "url")


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _doi_display(doi: str | None) -> str:
    """Render a DOI as a clickable Markdown link, or a placeholder."""
    return _DOI_LINK.format(doi=doi) if doi else "Not identified"


def _or_unknown(value: object, fallback: str = "Unknown") -> str:
    return str(value) if value else fallback


def _year_link(year: int | None) -> str:
    return f"[[{year}]]" if year else "Unknown"


def _yaml_year(year: int | None) -> str:
    return str(year) if year else "null"


def _placeholder_value(citation: CitationRecord, key: str) -> str:
    return _sanitize_inline(citation.get(key) or "")


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


def write_reference_placeholder(
    vault: Path, citation: CitationRecord, citing_title: str, citing_paper_id: str
) -> Path:
    """Create or refresh a placeholder, preserving notes and accumulating cites."""
    doi = normalize_doi(citation.get("doi"))
    raw = _sanitize_inline(citation.get("raw_reference", ""))
    title = _sanitize_inline(citation.get("title") or raw or "Unknown reference")
    paper_id = doi_work_id(doi) if doi else title_work_id(title)
    year = _publication_year(citation, raw)
    link = f"[[{_sanitize_inline(citing_title)} ({citing_paper_id})]]"

    placeholders_dir = vault / "11 Citation Placeholders"
    placeholders_dir.mkdir(parents=True, exist_ok=True)
    path = placeholders_dir / note_filename(title, paper_id)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    cited_by = _cited_by_links(existing)
    if link not in cited_by:
        cited_by.append(link)
    notes = _extract_section(existing, "My notes") or _DEFAULT_PLACEHOLDER_NOTES

    content = _render_reference_placeholder(
        citation=citation,
        paper_id=paper_id,
        title=title,
        doi=doi,
        raw=raw,
        year=year,
        cited_by=cited_by,
        notes=notes,
    )
    atomic_write_text(path, content)
    return path


def _publication_year(citation: CitationRecord, raw: str) -> int | None:
    """Use GROBID's year first, then fall back to the raw reference text."""
    value = citation.get("year")
    if isinstance(value, int) and 1000 <= value <= 2999:
        return value
    candidates = (str(value or ""), raw)
    for pattern in (_PARENTHESIZED_YEAR, _LOOSE_YEAR):
        for source in candidates:
            match = pattern.search(source)
            if match and 1000 <= int(match.group(1)) <= 2999:
                return int(match.group(1))
    return None


def _cited_by_links(content: str) -> list[str]:
    """Return previously recorded citing-paper links in source order."""
    match = _CITED_BY_LINE.search(content)
    return _OBSIDIAN_LINK.findall(match.group(1)) if match else []


def _fence_safe(value: str) -> str:
    """Keep arbitrary extracted text from terminating a Markdown fence."""
    return value.replace("~~~", "~ ~ ~")


def _render_reference_placeholder(
    *,
    citation: CitationRecord,
    paper_id: str,
    title: str,
    doi: str | None,
    raw: str,
    year: int | None,
    cited_by: list[str],
    notes: str,
) -> str:
    """Render one placeholder with all GROBID fields and a debug dump."""
    authors = [_sanitize_inline(author) for author in citation.get("authors") or []]
    values = {key: _placeholder_value(citation, key) for key in _PLACEHOLDER_FIELDS}
    fields = (
        ("Authors", _or_unknown(", ".join(authors))),
        ("Year", _year_link(year)),
        ("Venue", _or_unknown(values["venue"])),
        ("Volume", _or_unknown(values["volume"])),
        ("Issue", _or_unknown(values["issue"])),
        ("Pages", _or_unknown(values["pages"])),
        ("Publisher", _or_unknown(values["publisher"])),
        ("DOI", _doi_display(doi)),
        ("URL", _or_unknown(values["url"], "Not available")),
        ("Cited by", ", ".join(cited_by)),
        ("Citation as extracted", raw),
    )
    metadata = "\n".join(f"- **{label}:** {value}" for label, value in fields)
    dump = _grobid_dump(citation, raw, doi)
    tei = _or_unknown(citation.get("grobid", {}).get("tei"), "No raw TEI stored.")
    dump_json = _fence_safe(json.dumps(dump, indent=2, ensure_ascii=False, sort_keys=True))
    return f"""---
type: paper-placeholder
work_id: {_yaml_quote(paper_id)}
doi: {_yaml_quote(doi or "")}
title: {_yaml_quote(title)}
year: {_yaml_year(year)}
authors: {json.dumps(authors, ensure_ascii=False)}
venue: {_yaml_quote(values["venue"])}
volume: {_yaml_quote(values["volume"])}
issue: {_yaml_quote(values["issue"])}
pages: {_yaml_quote(values["pages"])}
publisher: {_yaml_quote(values["publisher"])}
url: {_yaml_quote(values["url"])}
cited_by: {json.dumps(cited_by, ensure_ascii=False)}
status: reference-placeholder
---

# {title}

This placeholder was created from a citation. Ingest the paper to replace it with a full paper note.

{metadata}

## GROBID Dump

Parsed fields reported by GROBID:

~~~json
{dump_json}
~~~

Raw TEI for this citation:

~~~xml
{tei}
~~~

## My notes

{notes}
"""


def _grobid_dump(
    citation: CitationRecord, raw: str, doi: str | None
) -> dict[str, object]:
    """Merge stored GROBID fields with any values added during resolution."""
    dump: dict[str, object] = dict(citation.get("grobid") or {})
    fallbacks = {
        "raw_reference": raw,
        "doi": doi,
        "title": citation.get("title"),
        "authors": citation.get("authors") or [],
        "year": citation.get("year"),
        "venue": citation.get("venue"),
        "volume": citation.get("volume"),
        "issue": citation.get("issue"),
        "pages": citation.get("pages"),
        "publisher": citation.get("publisher"),
        "url": citation.get("url"),
    }
    for key, value in fallbacks.items():
        if dump.get(key) is None and value is not None:
            dump[key] = value
    for key in ("target_id", "target_note", "status", "confidence", "evidence"):
        value = citation.get(key)
        if value is not None:
            dump[key] = str(value) if key == "target_note" else value
    return dump



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


def _extract_section(content: str, heading: str) -> str:
    """Return the body of one level-two heading, or an empty string."""
    marker = f"## {heading}"
    if marker not in content:
        return ""
    remainder = content.split(marker, 1)[1]
    next_heading = re.search(r"\n## ", remainder)
    body = remainder[: next_heading.start()] if next_heading else remainder
    return body.strip()


def _read_human_sections(path: Path) -> dict[str, str]:
    """Capture human-authored sections so refreshes never clobber them."""
    if not path.exists():
        return {}
    existing = path.read_text(encoding="utf-8")
    return {
        heading: body
        for heading in _SECTION_HEADINGS
        if (body := _extract_section(existing, heading))
    }


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
    summary_content = _sanitize_block(
        summary or human_sections.get("Summary") or _SUMMARY_FALLBACK
    )
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
