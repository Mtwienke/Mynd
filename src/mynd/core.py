from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree as ET

from ai.models import build_text_generator
from ai.runtime import MacThermalMonitor, RuntimeControl
from ai.summarize import SummaryWriter
from ingestion.extract_references import extract_grobid_references
from ingestion.extract_text import extract_text_and_doi
from ingestion.text import save_extracted_text, text_without_references
from mynd.contracts import (
    CitationRecord,
    IngestionFailure,
    IngestionResult,
    IngestionSuccess,
    PaperMetadata,
)
from mynd.files import sha256_file
from mynd.identity import doi_work_id, file_work_id
from mynd.settings import Settings
from resolution.citations import resolve_extracted_references
from resolution.crossref import CrossrefClient, metadata_from_crossref
from storage import repositories
from storage.database import initialize_database
from vault.note_writer import (
    PaperNote,
    link_summary_citations,
    write_paper_note,
    write_reference_placeholder,
)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback_url(url: str) -> bool:
    """Return True when a service URL points at this machine."""
    return (urlparse(url).hostname or "") in _LOOPBACK_HOSTS


@dataclass(frozen=True)
class _PreparedDocument:
    """Branch-independent facts needed to finalize one ingested PDF."""

    paper_id: str
    metadata: PaperMetadata
    source_pdf: Path
    text: str
    status: str
    is_refresh: bool
    metadata_error: str | None = None


def ingest_pdf(
    pdf_path: Path,
    settings: Settings,
    crossref: CrossrefClient | None = None,
    progress: Callable[[str], None] | None = None,
    summarizer: SummaryWriter | None = None,
) -> IngestionSuccess:
    """Ingest or refresh one PDF; the caller must have initialized the schema."""
    progress = progress or (lambda _message: None)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF file: {pdf_path}")

    digest = sha256_file(pdf_path)
    settings.pdf_store_path.mkdir(parents=True, exist_ok=True)
    stored_pdf = settings.pdf_store_path / f"{digest[:16]}{pdf_path.suffix.lower()}"

    existing = repositories.document_for_digest(settings.database_path, digest)
    if existing:
        prepared = _prepare_refresh(pdf_path, settings, existing, stored_pdf, progress)
    else:
        prepared = _prepare_new(pdf_path, settings, crossref, digest, stored_pdf, progress)
    return _finalize_ingestion(settings, crossref, summarizer, progress, prepared)


def _prepare_refresh(
    pdf_path: Path,
    settings: Settings,
    existing: repositories.StoredDocument,
    stored_pdf: Path,
    progress: Callable[[str], None],
) -> _PreparedDocument:
    """Reuse stored text and metadata for an already-ingested document."""
    document_id, paper_id, stored_path = existing
    source_pdf = Path(stored_path)
    if not source_pdf.is_file():
        shutil.copy2(pdf_path, stored_pdf)
        source_pdf = stored_pdf
        repositories.update_document_path(settings.database_path, document_id, source_pdf)

    text = repositories.extracted_text_for_document(settings.database_path, document_id)
    metadata = repositories.metadata_for_paper(settings.database_path, paper_id) or {
        "title": pdf_path.stem,
        "authors": [],
    }
    progress(f"Already ingested: {pdf_path.name}; refreshing note and references")
    return _PreparedDocument(
        paper_id=paper_id,
        metadata=metadata,
        source_pdf=source_pdf,
        text=text,
        status="already_ingested",
        is_refresh=True,
    )


def _prepare_new(
    pdf_path: Path,
    settings: Settings,
    crossref: CrossrefClient | None,
    digest: str,
    stored_pdf: Path,
    progress: Callable[[str], None],
) -> _PreparedDocument:
    """Extract, enrich, and persist a PDF seen for the first time."""
    if not stored_pdf.exists():
        progress("Copying PDF into the library")
        shutil.copy2(pdf_path, stored_pdf)
    source_pdf = stored_pdf

    progress("Extracting text and DOI from PDF")
    text, doi = extract_text_and_doi(source_pdf)
    metadata: PaperMetadata = {
        "doi": doi,
        "title": pdf_path.stem,
        "authors": [],
        "year": None,
        "venue": None,
        "url": None,
        "abstract": None,
        "type": None,
    }
    metadata_error = _enrich_metadata(metadata, doi, crossref, progress)

    paper_id = doi_work_id(metadata.get("doi")) or file_work_id(digest)
    if metadata.get("doi"):
        existing = repositories.paper_for_doi(settings.database_path, metadata["doi"])
        if existing is not None:
            paper_id = existing.id
    repositories.save_ingestion(
        settings.database_path,
        paper_id,
        metadata,
        repositories.IngestedDocument(
            digest=digest,
            filename=pdf_path.name,
            stored_path=source_pdf,
            text=text,
        ),
    )
    return _PreparedDocument(
        paper_id=paper_id,
        metadata=metadata,
        source_pdf=source_pdf,
        text=text,
        status="ingested",
        is_refresh=False,
        metadata_error=metadata_error,
    )


def _enrich_metadata(
    metadata: PaperMetadata,
    doi: str | None,
    crossref: CrossrefClient | None,
    progress: Callable[[str], None],
) -> str | None:
    """Fill metadata from Crossref, returning an error message on lookup failure."""
    if not (crossref and doi):
        return None
    try:
        progress(f"Looking up DOI metadata: {doi}")
        item = crossref.by_doi(doi)
        if item:
            metadata.update(
                {key: value for key, value in metadata_from_crossref(item).items()
                 if value is not None}
            )
    except (httpx.HTTPError, ValueError) as exc:
        return str(exc)
    return None


def _finalize_ingestion(
    settings: Settings,
    crossref: CrossrefClient | None,
    summarizer: SummaryWriter | None,
    progress: Callable[[str], None],
    prepared: _PreparedDocument,
) -> IngestionSuccess:
    """Persist extracted text, resolve references, summarize, and write the note."""
    extracted_text_path = save_extracted_text(settings.data_path, prepared.source_pdf, prepared.text)
    progress(f"Saved full extracted text: {extracted_text_path}")

    citations, citation_errors, references_error = _resolve_references(
        settings, crossref, prepared, progress
    )

    summary = None
    if summarizer and prepared.text:
        summary_text = text_without_references(prepared.text)
        progress(
            f"Summarizing paper text ({len(summary_text):,} of "
            f"{len(prepared.text):,} characters; terminal references excluded)"
        )
        summary = summarizer.summarize(summary_text, prepared.metadata, progress=progress)
        citations = summarizer.annotate_references(summary_text, citations, prepared.metadata)
        progress("Summary generated")

    for citation in citations:
        if not citation.get("target_note"):
            citation["placeholder_note"] = str(
                write_reference_placeholder(settings.vault_path, citation)
            )
    summary = link_summary_citations(summary, citations, settings.vault_path)

    progress("Writing Obsidian note")
    note = write_paper_note(
        PaperNote(
            vault=settings.vault_path,
            paper_id=prepared.paper_id,
            metadata=prepared.metadata,
            source_pdf=prepared.source_pdf,
            citations=citations,
            summary=summary,
        )
    )
    repositories.set_paper_note_path(settings.database_path, prepared.paper_id, note)

    result: IngestionSuccess = {
        "status": prepared.status,
        "paper_id": prepared.paper_id,
        "doi": prepared.metadata.get("doi"),
        "title": prepared.metadata.get("title"),
        "note": note,
        "references_extracted": len(citations),
        "extracted_text": extracted_text_path,
    }
    if prepared.metadata_error:
        result["metadata_error"] = prepared.metadata_error
    if references_error:
        result["references_error"] = references_error
    if citation_errors:
        result["citation_errors"] = citation_errors
    return result


def _resolve_references(
    settings: Settings,
    crossref: CrossrefClient | None,
    prepared: _PreparedDocument,
    progress: Callable[[str], None],
) -> tuple[list[CitationRecord], list[str], str | None]:
    """Resolve bibliography entries, falling back to stored citations on refresh."""
    stored = (
        repositories.citations_for_paper(settings.database_path, prepared.paper_id)
        if prepared.is_refresh
        else []
    )
    if not settings.use_grobid:
        return stored, [], None
    if not settings.allow_remote_services and not _is_loopback_url(settings.grobid_url):
        return stored, [], (
            "Refusing to send PDFs to a non-local GROBID service: "
            f"{settings.grobid_url}. Set allow_remote_services = true to opt in."
        )
    try:
        progress("Extracting bibliography with GROBID")
        references = extract_grobid_references(prepared.source_pdf, settings.grobid_url)
        citations, citation_errors = resolve_extracted_references(
            references,
            lambda doi: _paper_location(settings, doi),
            crossref,
        )
        repositories.insert_resolved_citations(
            settings.database_path, prepared.paper_id, citations
        )
        return citations, citation_errors, None
    except (httpx.HTTPError, ET.ParseError, OSError) as exc:
        return stored, [], _reference_error_detail(exc)


def _paper_location(settings: Settings, doi: str) -> tuple[str, str | None] | None:
    found = repositories.paper_for_doi(settings.database_path, doi)
    return (found.id, found.note_path) if found else None


def _reference_error_detail(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text.strip()
        detail = f"GROBID returned HTTP {exc.response.status_code}"
        if body:
            detail += f": {body[:1000]}"
        return detail
    return f"GROBID reference extraction failed: {exc}"


def _stderr_progress(message: str) -> None:
    print(f"[ingest] {message}", file=sys.stderr, flush=True)


def _prepare_runtime(
    settings: Settings, progress: Callable[[str], None], verbose: bool
) -> tuple[RuntimeControl, SummaryWriter | None]:
    """Start runtime control and resolve the summarizer for this batch."""
    runtime_control = RuntimeControl(settings.mode_file)
    progress(f"Inference mode: {runtime_control.mode()}")
    if not MacThermalMonitor.supported():
        progress(
            f"Automatic thermal response is not implemented for {sys.platform}; "
            "mode limits still work"
        )
    if verbose:
        runtime_control.report_thermal_state("ingestion start", progress)

    generator = build_text_generator(
        runtime_control=runtime_control,
        progress=progress,
        verbose=verbose,
    )
    progress(f"Checking summarization model availability ({generator.model})")
    if not generator.ensure_model():
        progress(
            f"Model {generator.model} is unavailable; continuing ingestion without summarization"
        )
        return runtime_control, None
    progress(f"Summarization model ready: {generator.model}")
    return runtime_control, SummaryWriter(generator)


def _ingest_batch(
    pdfs: list[Path],
    settings: Settings,
    crossref: CrossrefClient | None,
    summarizer: SummaryWriter | None,
    progress: Callable[[str], None],
    runtime_control: RuntimeControl,
    verbose: bool,
) -> list[IngestionResult]:
    """Ingest each PDF, isolating failures so one document cannot abort the batch."""
    results: list[IngestionResult] = []
    for index, pdf in enumerate(pdfs, start=1):
        progress_label = f"PDF {index}/{len(pdfs)}"
        progress(f"{progress_label}: {pdf.name}")
        try:
            result: IngestionResult = ingest_pdf(
                pdf, settings, crossref, progress=progress, summarizer=summarizer
            )
            progress(f"Finished {progress_label}: {pdf.name} ({result['status']})")
        except Exception as exc:  # noqa: BLE001 - isolate failures per document
            failure: IngestionFailure = {
                "status": "failed",
                "filename": pdf.name,
                "error": str(exc),
            }
            _record_failure(settings, pdf, exc)
            result = failure
            progress(f"Failed {progress_label}: {pdf.name}: {exc}")
            progress_label += " failure"
        results.append(result)
        if verbose:
            runtime_control.report_thermal_state(progress_label, progress)
    return results


def _record_failure(settings: Settings, pdf: Path, exc: Exception) -> None:
    """Best-effort failure logging that cannot mask the original exception."""
    try:
        digest = sha256_file(pdf)
    except OSError:
        digest = ""
    repositories.record_ingestion_failure(
        settings.database_path, digest, pdf.name, str(exc)
    )


def ingest_inbox(
    settings: Settings,
    crossref: CrossrefClient | None = None,
    *,
    verbose: bool = False,
) -> list[IngestionResult]:
    """Process every PDF in the configured inbox."""
    notify = _stderr_progress
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    settings.vault_path.mkdir(parents=True, exist_ok=True)
    initialize_database(settings.database_path)

    pdfs = sorted(settings.inbox_path.glob("*.pdf"))
    notify(f"Found {len(pdfs)} PDF{'s' if len(pdfs) != 1 else ''} in {settings.inbox_path}")
    if not pdfs:
        return []

    runtime_control, summarizer = _prepare_runtime(settings, notify, verbose)
    try:
        return _ingest_batch(
            pdfs, settings, crossref, summarizer, notify, runtime_control, verbose
        )
    finally:
        if summarizer is not None:
            summarizer.generator.close()
