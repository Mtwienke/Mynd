from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
import pymupdf

# Import model management
from ai.models import get_model_manager
from ai.summarize import SummaryWriter

DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)


@dataclass(frozen=True)
class Settings:
    vault_path: Path
    data_path: Path
    inbox_path: Path
    pdf_store_path: Path
    database_path: Path
    grobid_url: str = "http://localhost:8070"
    use_grobid: bool = True
    crossref_email: str = "you@example.com"
    use_local_llm: bool = True  # New setting for LLM preference


def load_settings(path: Path) -> Settings:
    import tomllib

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent

    def resolve(value: str) -> Path:
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else (base / candidate).resolve()

    return Settings(
        vault_path=resolve(raw["vault_path"]),
        data_path=resolve(raw["data_path"]),
        inbox_path=resolve(raw["inbox_path"]),
        pdf_store_path=resolve(raw["pdf_store_path"]),
        database_path=resolve(raw["database_path"]),
        grobid_url=raw.get("grobid_url", "http://localhost:8070").rstrip("/"),
        use_grobid=bool(raw.get("use_grobid", True)),
        crossref_email=raw.get("crossref_email", "you@example.com"),
        use_local_llm=raw.get("use_local_llm", True),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    value = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", value, flags=re.I)
    match = DOI_RE.search(value.rstrip(" .,:;"))
    return match.group(0).rstrip(" .,:;") if match else None


def extract_pdf(path: Path) -> tuple[str, str | None]:
    """Return extracted page text and the first DOI found in metadata/text."""
    with pymupdf.open(path) as doc:
        metadata = doc.metadata or {}
        parts = [page.get_text("text") for page in doc]
        text = "\n\n".join(parts)
        candidates = [metadata.get("subject", ""), metadata.get("keywords", ""), text[:30000]]
        doi = next((found for item in candidates if (found := normalize_doi(item))), None)
        return text, doi


def _save_extracted_text(data_path: Path, stored_pdf: Path, text: str) -> Path:
    """Persist the full PDF text extraction for inspection and troubleshooting."""
    output_dir = data_path / "extracted_text"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{stored_pdf.stem}.txt"
    output.write_text(text, encoding="utf-8")
    return output


def _text_without_references(text: str) -> str:
    """Exclude a terminal bibliography from the text sent to the summarizer."""
    heading = re.compile(
        r"(?im)^[ \t]*(?:REFERENCES(?:[ \t]+AND[ \t]+NOTES)?|BIBLIOGRAPHY)[ \t]*$"
    )
    for match in heading.finditer(text):
        if match.start() >= len(text) // 2:
            return text[:match.start()].rstrip()
    return text


class CrossrefClient:
    def __init__(self, email: str = "you@example.com", timeout: float = 15.0):
        self.client = httpx.Client(
            base_url="https://api.crossref.org",
            params={"mailto": email},
            timeout=timeout,
            headers={"User-Agent": "Mynd/0.1 (personal research library)"},
        )

    def by_doi(self, doi: str) -> dict[str, Any] | None:
        response = self.client.get(f"/works/{quote(doi, safe='')}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()["message"]

    def search_title(self, title: str) -> list[dict[str, Any]]:
        response = self.client.get("/works", params={"query.title": title, "rows": 5})
        response.raise_for_status()
        return response.json()["message"].get("items", [])

    def close(self) -> None:
        self.client.close()


def metadata_from_crossref(item: dict[str, Any]) -> dict[str, Any]:
    authors = []
    for author in item.get("author", []):
        name = " ".join(p for p in [author.get("given"), author.get("family")] if p)
        if name:
            authors.append(name)
    date_parts = (
        item.get("published-print", {}).get("date-parts")
        or item.get("published-online", {}).get("date-parts")
        or item.get("issued", {}).get("date-parts")
        or []
    )
    year = date_parts[0][0] if date_parts and date_parts[0] else None
    title_list = item.get("title") or []
    container_list = item.get("container-title") or []
    return {
        "doi": normalize_doi(item.get("DOI")),
        "title": title_list[0] if title_list else None,
        "authors": authors,
        "year": year,
        "venue": container_list[0] if container_list else None,
        "url": item.get("URL"),
        "abstract": item.get("abstract"),
        "type": item.get("type"),
    }


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS papers (
                id TEXT PRIMARY KEY,
                doi TEXT UNIQUE,
                title TEXT NOT NULL,
                year INTEGER,
                authors_json TEXT NOT NULL DEFAULT '[]',
                venue TEXT,
                url TEXT,
                abstract TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                note_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                paper_id TEXT,
                sha256 TEXT NOT NULL UNIQUE,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                extracted_text TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(paper_id) REFERENCES papers(id)
            );
            CREATE TABLE IF NOT EXISTS citations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_paper_id TEXT NOT NULL,
                target_paper_id TEXT,
                raw_reference TEXT NOT NULL,
                cited_doi TEXT,
                status TEXT NOT NULL DEFAULT 'unresolved',
                confidence REAL NOT NULL DEFAULT 0,
                evidence TEXT,
                FOREIGN KEY(source_paper_id) REFERENCES papers(id),
                FOREIGN KEY(target_paper_id) REFERENCES papers(id),
                UNIQUE(source_paper_id, raw_reference)
            );
            CREATE TABLE IF NOT EXISTS ingestion_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def _paper_id(doi: str | None, digest: str) -> str:
    if doi:
        return "doi-" + hashlib.sha1(doi.lower().encode()).hexdigest()[:12]
    return "paper-" + digest[:16]


def _ensure_reference_stub(vault: Path, citation: dict[str, Any]) -> Path:
    """Create a stable, clearly marked note so unresolved citations are linkable."""
    doi = normalize_doi(citation.get("doi"))
    raw = citation.get("raw_reference", "")
    title = citation.get("title") or raw or "Unknown reference"
    identity = doi.lower() if doi else title.casefold()
    paper_id = _paper_id(doi, hashlib.sha1(identity.encode("utf-8")).hexdigest())
    metadata = {"title": title, "doi": doi, "authors": [], "year": None,
                "venue": None, "abstract": None, "url": f"https://doi.org/{doi}" if doi else None}
    placeholders_dir = vault / "11 Citation Placeholders"
    placeholders_dir.mkdir(parents=True, exist_ok=True)
    expected = placeholders_dir / f"{_slug(title)} ({paper_id[-12:]}).md"
    if expected.exists():
        return expected
    raw = citation.get("raw_reference", "")
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
    expected.write_text(content, encoding="utf-8")
    return expected


def _obsidian_link(vault: Path, target: str | Path) -> str:
    """Build a vault-relative Obsidian link to an existing note path."""
    path = _migrate_bracketed_note_path(Path(target))
    try:
        link_path = path.resolve().relative_to(vault.resolve()).with_suffix("")
    except ValueError:
        link_path = Path(path.stem)
    return f"[[{link_path.as_posix()}]]"


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


def _link_summary_citations(summary: str | None, citations: list[dict[str, Any]], vault: Path) -> str | None:
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
            if not note:
                return number_match.group(0)
            return _obsidian_link(vault, note)

        converted = re.sub(r"\d+", replace_number, group)
        return f"({converted})"

    citation_group = r"(\d+(?:\s*[,;–-]\s*\d+)*)"
    summary = re.sub(r"(?<!\[)\[\[" + citation_group + r"\]\](?!\])", replace_group, summary)
    return re.sub(r"(?<!\[)\[" + citation_group + r"\](?!\])", replace_group, summary)


def _slug(text: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|#^\[\]]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:100] or "Untitled Paper"


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def write_note(vault: Path, paper_id: str, metadata: dict[str, Any], source_pdf: Path,
               citations: list[dict[str, Any]] | None = None,
               summary: str | None = None) -> Path:
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
        target_path = citation.get("target_note")
        raw = citation.get("raw_reference", "").strip()
        importance = citation.get("importance")
        direction = citation.get("direction")
        similarity = citation.get("similarity")
        labels = []
        if importance:
            labels.append(f"**{importance}**")
        if direction:
            labels.append(f"_{direction}_")
        if similarity:
            labels.append(f"Similarity: {similarity}")
        annotation = " · ".join(labels)
        if target_path:
            citation_lines.append(f"- {_obsidian_link(vault, target_path)}" + (f" — {annotation}" if annotation else ""))
        elif citation.get("placeholder_note"):
            citation_lines.append(f"- {_obsidian_link(vault, citation['placeholder_note'])}" + (f" — {annotation}" if annotation else ""))
        else:
            status = citation.get("status", "unresolved")
            citation_lines.append(f"- **{status.title()} reference:** {raw}" + (f" — {annotation}" if annotation else ""))
    cites = "\n".join(citation_lines) if citation_lines else "_No references extracted yet._"

    # Preserve human-authored sections if this note already exists.
    human_sections = {}
    if path.exists():
        old = path.read_text(encoding="utf-8")
        for heading in ("Summary", "My notes", "Questions", "Reading log"):
            marker = f"## {heading}"
            if marker in old:
                section = old.split(marker, 1)[1]
                next_heading = re.search(r"\n## ", section)
                human_sections[heading] = section[:next_heading.start()].strip() if next_heading else section.strip()

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


def extract_grobid_references(pdf_path: Path, base_url: str = "http://localhost:8070",
                              timeout: float = 90.0) -> list[dict[str, Any]]:
    """Extract bibliography entries via a running GROBID service."""
    with pdf_path.open("rb") as handle:
        response = httpx.post(
            f"{base_url.rstrip('/')}/api/processReferences",
            files={"input": (pdf_path.name, handle, "application/pdf")},
            data={"consolidateCitations": "0"},
            timeout=timeout,
        )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    ns = {"tei": "http://www.tei-c.org/ns/1.0"}
    results = []
    for node in root.findall(".//tei:biblStruct", ns):
        raw = " ".join(" ".join(node.itertext()).split())
        doi = None
        for idno in node.findall(".//tei:idno", ns):
            if idno.attrib.get("type", "").lower() == "doi":
                doi = normalize_doi(idno.text)
                break
        title_node = node.find(".//tei:analytic/tei:title", ns)
        title = " ".join(title_node.itertext()).strip() if title_node is not None else None
        results.append({"raw_reference": raw, "doi": doi, "title": title})
    return results


def _extract_and_store_references(pdf_path: Path, paper_id: str, settings: Settings) -> list[dict[str, Any]]:
    """Extract bibliography entries and persist them, returning note-ready records."""
    refs = extract_grobid_references(pdf_path, settings.grobid_url)
    citations = [{
        "raw_reference": ref["raw_reference"],
        "doi": ref.get("doi"),
        "target_id": None,
        "target_note": None,
        "status": "unresolved",
        "confidence": 0.0,
    } for ref in refs]
    with sqlite3.connect(settings.database_path) as db:
        for cite in citations:
            db.execute(
                """INSERT OR IGNORE INTO citations
                (source_paper_id, target_paper_id, raw_reference, cited_doi, status, confidence, evidence)
                VALUES(?,?,?,?,?,?,?)""",
                (paper_id, None, cite["raw_reference"], cite["doi"], cite["status"],
                 cite["confidence"], "GROBID bibliography extraction"),
            )
        db.commit()
    return citations


def _citation_records(settings: Settings, paper_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(settings.database_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT c.raw_reference, c.cited_doi AS doi, c.status,
                      target.note_path AS target_note
               FROM citations c LEFT JOIN papers target ON target.id=c.target_paper_id
               WHERE c.source_paper_id=? ORDER BY c.id""",
            (paper_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def ingest_pdf(
    pdf_path: Path,
    settings: Settings,
    crossref: CrossrefClient | None = None,
    progress: Callable[[str], None] | None = None,
    summarizer: SummaryWriter | None = None,
) -> dict[str, Any]:
    """Ingest one PDF. Safe to call repeatedly for the same file."""
    report = progress or (lambda _message: None)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF file: {pdf_path}")

    init_db(settings.database_path)
    digest = sha256_file(pdf_path)
    settings.pdf_store_path.mkdir(parents=True, exist_ok=True)
    stored_pdf = settings.pdf_store_path / f"{digest[:16]}{pdf_path.suffix.lower()}"

    with sqlite3.connect(settings.database_path) as db:
        existing = db.execute(
            "SELECT id, paper_id, stored_path FROM documents WHERE sha256=?", (digest,)
        ).fetchone()
        if existing:
            report(f"Already ingested: {pdf_path.name}; refreshing note and references")
            paper_id = existing[1]
            existing_pdf_path = Path(existing[2])
            if not existing_pdf_path.is_file():
                # The configured PDF store may have moved since this row was created.
                # The inbox copy is the same PDF (the lookup above matched its SHA-256).
                shutil.copy2(pdf_path, stored_pdf)
                existing_pdf_path = stored_pdf
                with sqlite3.connect(settings.database_path) as db:
                    db.execute("UPDATE documents SET stored_path=? WHERE id=?",
                               (str(existing_pdf_path), existing[0]))
                    db.commit()
            extracted_text_row = db.execute(
                "SELECT extracted_text FROM documents WHERE id=?", (existing[0],)
            ).fetchone()
            extracted_text = extracted_text_row[0] if extracted_text_row else ""
            extracted_text_path = _save_extracted_text(settings.data_path, existing_pdf_path, extracted_text)
            report(f"Saved full extracted text: {extracted_text_path}")
            paper = db.execute("SELECT doi, title, year, authors_json, venue, url, abstract, metadata_json FROM papers WHERE id=?",
                               (paper_id,)).fetchone()
            import json
            metadata = {
                "doi": paper[0], "title": paper[1], "year": paper[2],
                "authors": json.loads(paper[3]), "venue": paper[4], "url": paper[5],
                "abstract": paper[6], **json.loads(paper[7]),
            }
            extraction_error = None
            if settings.use_grobid:
                try:
                    report("Extracting bibliography with GROBID")
                    _extract_and_store_references(existing_pdf_path, paper_id, settings)
                except (httpx.HTTPError, ET.ParseError, OSError) as exc:
                    extraction_error = _reference_error_detail(exc)
            summary = None
            citations = _citation_records(settings, paper_id)
            if summarizer and extracted_text:
                summary_text = _text_without_references(extracted_text)
                report(
                    f"Summarizing paper text ({len(summary_text):,} of "
                    f"{len(extracted_text):,} characters; terminal references excluded)"
                )
                summary = summarizer.summarize(summary_text, metadata, progress=report)
                citations = summarizer.annotate_references(summary_text, citations, metadata)
                report("Summary generated")
            for citation in citations:
                if not citation.get("target_note"):
                    citation["placeholder_note"] = str(_ensure_reference_stub(settings.vault_path, citation))
            summary = _link_summary_citations(summary, citations, settings.vault_path)
            report("Writing Obsidian note")
            note = write_note(settings.vault_path, paper_id, metadata, existing_pdf_path,
                              citations, summary)
            db.execute("UPDATE papers SET note_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (str(note), paper_id))
            db.commit()
            result = {"status": "already_ingested", "paper_id": paper_id, "note": note,
                      "references_extracted": len(_citation_records(settings, paper_id)),
                      "extracted_text": extracted_text_path}
            if extraction_error:
                result["references_error"] = extraction_error
            return result

    if not stored_pdf.exists():
        report("Copying PDF into the library")
        shutil.copy2(pdf_path, stored_pdf)
    report("Extracting text and DOI from PDF")
    text, doi = extract_pdf(stored_pdf)
    extracted_text_path = _save_extracted_text(settings.data_path, stored_pdf, text)
    report(f"Saved full extracted text: {extracted_text_path}")
    metadata: dict[str, Any] = {
        "doi": doi, "title": pdf_path.stem, "authors": [], "year": None,
        "venue": None, "url": None, "abstract": None, "type": None,
    }
    if crossref and doi:
        try:
            report(f"Looking up DOI metadata: {doi}")
            item = crossref.by_doi(doi)
            if item:
                metadata.update({k: v for k, v in metadata_from_crossref(item).items() if v is not None})
        except (httpx.HTTPError, ValueError):
            # Preserve the local PDF even if metadata services are unavailable.
            pass

    paper_id = _paper_id(metadata.get("doi"), digest)
    import json
    with sqlite3.connect(settings.database_path) as db:
        # A DOI may already exist from a different PDF.
        if metadata.get("doi"):
            existing_paper = db.execute("SELECT id FROM papers WHERE lower(doi)=lower(?)",
                                        (metadata["doi"],)).fetchone()
            if existing_paper:
                paper_id = existing_paper[0]
        db.execute(
            """INSERT INTO papers(id, doi, title, year, authors_json, venue, url, abstract, metadata_json)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 doi=COALESCE(excluded.doi, papers.doi),
                 title=CASE WHEN papers.title IS NULL OR papers.title='' THEN excluded.title ELSE papers.title END,
                 updated_at=CURRENT_TIMESTAMP""",
            (paper_id, metadata.get("doi"), metadata.get("title") or pdf_path.stem,
             metadata.get("year"), json.dumps(metadata.get("authors") or []), metadata.get("venue"),
             metadata.get("url"), metadata.get("abstract"),
             json.dumps({k: v for k, v in metadata.items() if k not in {"doi", "title", "year", "authors", "venue", "url", "abstract"}})),
        )
        db.execute(
            "INSERT OR IGNORE INTO documents(id, paper_id, sha256, original_name, stored_path, extracted_text) VALUES(?,?,?,?,?,?)",
            (digest[:24], paper_id, digest, pdf_path.name, str(stored_pdf), text),
        )
        db.execute(
            "INSERT INTO ingestion_jobs(sha256, filename, status) VALUES(?,?,?)",
            (digest, pdf_path.name, "ingested"),
        )
        db.commit()

    citations: list[dict[str, Any]] = []
    extraction_error = None
    if settings.use_grobid:
        try:
            report("Extracting bibliography with GROBID")
            refs = extract_grobid_references(stored_pdf, settings.grobid_url)
            for ref in refs:
                target_id = None
                target_note = None
                status = "unresolved"
                confidence = 0.0
                cited_doi = ref.get("doi")
                if cited_doi and crossref:
                    try:
                        target_meta_raw = crossref.by_doi(cited_doi)
                        if target_meta_raw:
                            target_meta = metadata_from_crossref(target_meta_raw)
                            with sqlite3.connect(settings.database_path) as db:
                                row = db.execute("SELECT id, note_path FROM papers WHERE lower(doi)=lower(?)",
                                                 (cited_doi,)).fetchone()
                            if row:
                                target_id, target_note = row
                                status, confidence = "resolved", 1.0
                            else:
                                status, confidence = "external", 0.95
                    except httpx.HTTPError:
                        pass
                citations.append({
                    "raw_reference": ref["raw_reference"], "doi": cited_doi,
                    "target_id": target_id, "target_note": target_note,
                    "status": status, "confidence": confidence,
                })
            with sqlite3.connect(settings.database_path) as db:
                for cite in citations:
                    db.execute(
                        """INSERT OR IGNORE INTO citations
                        (source_paper_id, target_paper_id, raw_reference, cited_doi, status, confidence, evidence)
                        VALUES(?,?,?,?,?,?,?)""",
                        (paper_id, cite["target_id"], cite["raw_reference"], cite["doi"],
                         cite["status"], cite["confidence"], "GROBID bibliography extraction"),
                    )
                db.commit()
        except (httpx.HTTPError, ET.ParseError, OSError) as exc:
            extraction_error = _reference_error_detail(exc)
            citations = []

    summary = None
    if summarizer:
        summary_text = _text_without_references(text)
        report(
            f"Summarizing paper text ({len(summary_text):,} of "
            f"{len(text):,} characters; terminal references excluded)"
        )
        summary = summarizer.summarize(summary_text, metadata, progress=report)
        citations = summarizer.annotate_references(summary_text, citations, metadata)
        report("Summary generated")

    for citation in citations:
        if not citation.get("target_note"):
            citation["placeholder_note"] = str(_ensure_reference_stub(settings.vault_path, citation))
    summary = _link_summary_citations(summary, citations, settings.vault_path)

    report("Writing Obsidian note")
    note = write_note(settings.vault_path, paper_id, metadata, stored_pdf, citations, summary)
    with sqlite3.connect(settings.database_path) as db:
        db.execute("UPDATE papers SET note_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (str(note), paper_id))
        db.commit()
    result = {
        "status": "ingested",
        "paper_id": paper_id,
        "doi": metadata.get("doi"),
        "title": metadata.get("title"),
        "note": note,
        "references_extracted": len(citations),
        "extracted_text": extracted_text_path,
    }
    if settings.use_grobid and not citations and extraction_error:
        result["references_error"] = extraction_error
    return result


def _reference_error_detail(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text.strip()
        detail = f"GROBID returned HTTP {exc.response.status_code}"
        if body:
            detail += f": {body[:1000]}"
        return detail
    return f"GROBID reference extraction failed: {exc}"


def process_inbox(settings: Settings, crossref: CrossrefClient | None = None) -> list[dict[str, Any]]:
    def report(message: str) -> None:
        print(f"[ingest] {message}", file=sys.stderr, flush=True)

    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    settings.vault_path.mkdir(parents=True, exist_ok=True)
    init_db(settings.database_path)

    pdfs = sorted(settings.inbox_path.glob("*.pdf"))
    report(f"Found {len(pdfs)} PDF{'s' if len(pdfs) != 1 else ''} in {settings.inbox_path}")
    if not pdfs:
        return []

    # Ensure the LLM is ready for future summarization.
    manager = get_model_manager(use_local=settings.use_local_llm)
    report(f"Checking summarization model availability ({manager.model})")
    if not manager.ensure_model():
        report(f"Model {manager.model} is unavailable; continuing ingestion without summarization")
        summarizer = None
    else:
        report(f"Summarization model ready: {manager.model}")
        summarizer = SummaryWriter(manager)

    results = []
    for index, pdf in enumerate(pdfs, start=1):
        report(f"PDF {index}/{len(pdfs)}: {pdf.name}")
        try:
            result = ingest_pdf(pdf, settings, crossref, progress=report, summarizer=summarizer)
            results.append(result)
            report(f"Finished PDF {index}/{len(pdfs)}: {pdf.name} ({result['status']})")
        except Exception as exc:
            with sqlite3.connect(settings.database_path) as db:
                db.execute(
                    "INSERT INTO ingestion_jobs(sha256, filename, status, error) VALUES(?,?,?,?)",
                    (sha256_file(pdf), pdf.name, "failed", str(exc)),
                )
            results.append({"status": "failed", "filename": pdf.name, "error": str(exc)})
            report(f"Failed PDF {index}/{len(pdfs)}: {pdf.name}: {exc}")
    return results


def write_review_queue(settings: Settings) -> Path:
    """Write unresolved/ambiguous citation records to a human-review note."""
    init_db(settings.database_path)
    output = settings.vault_path / "90 System" / "Citation Review Queue.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as db:
        rows = db.execute(
            """SELECT p.title, c.raw_reference, c.cited_doi, c.status
               FROM citations c JOIN papers p ON p.id=c.source_paper_id
               WHERE c.status IN ('unresolved', 'ambiguous', 'external')
               ORDER BY p.title"""
        ).fetchall()
    lines = ["# Citation Review Queue", "", "Review these references and link them to notes when appropriate.", ""]
    for title, raw, doi, status in rows:
        lines.append(f"- **{status}** — from *{title}*: {raw}")
        if doi:
            lines.append(f"  - DOI: https://doi.org/{doi}")
    if not rows:
        lines.append("_No pending citation records._")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output
