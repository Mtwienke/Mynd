from __future__ import annotations

import hashlib
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from defusedxml import ElementTree as ET

from ai.models import get_model_manager
from ai.runtime import MacThermalMonitor, RuntimeControl
from ai.summarize import SummaryWriter
from ingestion.extract_references import extract_grobid_references
from ingestion.extract_text import extract_pdf
from mynd.files import save_extracted_text, sha256_file, text_without_references
from mynd.settings import Settings
from resolution.citations import CitationRecord, resolve_extracted_references
from resolution.crossref import CrossrefClient, metadata_from_crossref
from resolution.metadata import PaperMetadata
from storage.database import initialize_database
from storage.repositories import (
    StoredDocument,
    citations_for_paper,
    document_for_digest,
    extracted_text_for_document,
    insert_resolved_citations,
    metadata_for_paper,
    paper_for_doi,
    paper_id_for_doi,
    record_ingestion_failure,
    save_ingestion,
    set_paper_note_path,
    update_document_path,
)
from vault.note_writer import (
    link_summary_citations,
    write_paper_note,
    write_reference_placeholder,
)


def _paper_id(doi: str | None, digest: str) -> str:
    if doi:
        return "doi-" + hashlib.sha1(
            doi.lower().encode(), usedforsecurity=False
        ).hexdigest()[:12]
    return "paper-" + digest[:16]


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
) -> dict[str, Any]:
    """Ingest or refresh one PDF, then run a shared summary and note finalization."""
    report = progress or (lambda _message: None)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF file: {pdf_path}")

    initialize_database(settings.database_path)
    digest = sha256_file(pdf_path)
    settings.pdf_store_path.mkdir(parents=True, exist_ok=True)
    stored_pdf = settings.pdf_store_path / f"{digest[:16]}{pdf_path.suffix.lower()}"

    existing = document_for_digest(settings.database_path, digest)
    if existing:
        prepared = _prepare_refresh(pdf_path, settings, existing, stored_pdf, report)
    else:
        prepared = _prepare_new(pdf_path, settings, crossref, digest, stored_pdf, report)
    return _finalize_ingestion(settings, crossref, summarizer, report, prepared)


def _prepare_refresh(
    pdf_path: Path,
    settings: Settings,
    existing: StoredDocument,
    stored_pdf: Path,
    report: Callable[[str], None],
) -> _PreparedDocument:
    """Reuse stored text and metadata for an already-ingested document."""
    document_id, paper_id, stored_path = existing
    source_pdf = Path(stored_path)
    if not source_pdf.is_file():
        shutil.copy2(pdf_path, stored_pdf)
        source_pdf = stored_pdf
        update_document_path(settings.database_path, document_id, source_pdf)

    text = extracted_text_for_document(settings.database_path, document_id)
    metadata = metadata_for_paper(settings.database_path, paper_id) or {
        "title": pdf_path.stem,
        "authors": [],
    }
    report(f"Already ingested: {pdf_path.name}; refreshing note and references")
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
    report: Callable[[str], None],
) -> _PreparedDocument:
    """Extract, enrich, and persist a PDF seen for the first time."""
    if not stored_pdf.exists():
        report("Copying PDF into the library")
        shutil.copy2(pdf_path, stored_pdf)
    source_pdf = stored_pdf

    report("Extracting text and DOI from PDF")
    text, doi = extract_pdf(source_pdf)
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
    metadata_error = _enrich_metadata(metadata, doi, crossref, report)

    paper_id = _paper_id(metadata.get("doi"), digest)
    if metadata.get("doi"):
        paper_id = paper_id_for_doi(settings.database_path, metadata["doi"]) or paper_id
    save_ingestion(
        settings.database_path, paper_id, metadata, digest, pdf_path.name, source_pdf, text
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
    report: Callable[[str], None],
) -> str | None:
    """Fill metadata from Crossref, returning an error message on lookup failure."""
    if not (crossref and doi):
        return None
    try:
        report(f"Looking up DOI metadata: {doi}")
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
    report: Callable[[str], None],
    prepared: _PreparedDocument,
) -> dict[str, Any]:
    """Persist extracted text, resolve references, summarize, and write the note."""
    extracted_text_path = save_extracted_text(settings.data_path, prepared.source_pdf, prepared.text)
    report(f"Saved full extracted text: {extracted_text_path}")

    citations, citation_errors, references_error = _resolve_references(
        settings, crossref, prepared, report
    )

    summary = None
    if summarizer and prepared.text:
        summary_text = text_without_references(prepared.text)
        report(
            f"Summarizing paper text ({len(summary_text):,} of "
            f"{len(prepared.text):,} characters; terminal references excluded)"
        )
        summary = summarizer.summarize(summary_text, prepared.metadata, progress=report)
        citations = summarizer.annotate_references(summary_text, citations, prepared.metadata)
        report("Summary generated")

    for citation in citations:
        if not citation.get("target_note"):
            citation["placeholder_note"] = str(
                write_reference_placeholder(settings.vault_path, citation)
            )
    summary = link_summary_citations(summary, citations, settings.vault_path)

    report("Writing Obsidian note")
    note = write_paper_note(
        settings.vault_path,
        prepared.paper_id,
        prepared.metadata,
        prepared.source_pdf,
        citations,
        summary,
    )
    set_paper_note_path(settings.database_path, prepared.paper_id, note)

    result = {
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
    report: Callable[[str], None],
) -> tuple[list[CitationRecord], list[str], str | None]:
    """Resolve bibliography entries, falling back to stored citations on refresh."""
    stored = (
        citations_for_paper(settings.database_path, prepared.paper_id)
        if prepared.is_refresh
        else []
    )
    if not settings.use_grobid:
        return stored, [], None
    try:
        report("Extracting bibliography with GROBID")
        references = extract_grobid_references(prepared.source_pdf, settings.grobid_url)
        citations, citation_errors = resolve_extracted_references(
            references,
            lambda doi: paper_for_doi(settings.database_path, doi),
            crossref,
        )
        insert_resolved_citations(settings.database_path, prepared.paper_id, citations)
        return citations, citation_errors, None
    except (httpx.HTTPError, ET.ParseError, OSError) as exc:
        return stored, [], _reference_error_detail(exc)


def _reference_error_detail(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text.strip()
        detail = f"GROBID returned HTTP {exc.response.status_code}"
        if body:
            detail += f": {body[:1000]}"
        return detail
    return f"GROBID reference extraction failed: {exc}"


def _stderr_report(message: str) -> None:
    print(f"[ingest] {message}", file=sys.stderr, flush=True)


def _prepare_runtime(
    settings: Settings, report: Callable[[str], None], verbose: bool
) -> tuple[RuntimeControl, SummaryWriter | None]:
    """Start runtime control and resolve the summarizer for this batch."""
    runtime_control = RuntimeControl(settings.data_path / ".mynd-mode")
    report(f"Inference mode: {runtime_control.mode()}")
    if settings.use_local_llm and not MacThermalMonitor.supported():
        report(
            f"Automatic thermal response is not implemented for {sys.platform}; "
            "mode limits still work"
        )
    if settings.use_local_llm and verbose:
        runtime_control.report_thermal_state("ingestion start", report)

    manager = get_model_manager(
        use_local=settings.use_local_llm,
        runtime_control=runtime_control,
        progress=report,
        verbose=verbose,
    )
    report(f"Checking summarization model availability ({manager.model})")
    if not manager.ensure_model():
        report(
            f"Model {manager.model} is unavailable; continuing ingestion without summarization"
        )
        return runtime_control, None
    report(f"Summarization model ready: {manager.model}")
    return runtime_control, SummaryWriter(manager)


def _ingest_batch(
    pdfs: list[Path],
    settings: Settings,
    crossref: CrossrefClient | None,
    summarizer: SummaryWriter | None,
    report: Callable[[str], None],
    runtime_control: RuntimeControl,
    verbose: bool,
) -> list[dict[str, Any]]:
    """Ingest each PDF, isolating failures so one document cannot abort the batch."""
    results: list[dict[str, Any]] = []
    for index, pdf in enumerate(pdfs, start=1):
        progress_label = f"PDF {index}/{len(pdfs)}"
        report(f"{progress_label}: {pdf.name}")
        try:
            result = ingest_pdf(
                pdf, settings, crossref, progress=report, summarizer=summarizer
            )
            report(f"Finished {progress_label}: {pdf.name} ({result['status']})")
        except Exception as exc:  # noqa: BLE001 - isolate failures per document
            record_ingestion_failure(
                settings.database_path, sha256_file(pdf), pdf.name, str(exc)
            )
            result = {"status": "failed", "filename": pdf.name, "error": str(exc)}
            report(f"Failed {progress_label}: {pdf.name}: {exc}")
            progress_label += " failure"
        results.append(result)
        if settings.use_local_llm and verbose:
            runtime_control.report_thermal_state(progress_label, report)
    return results


def ingest_inbox(
    settings: Settings,
    crossref: CrossrefClient | None = None,
    *,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """Process every PDF in the configured inbox."""
    report = _stderr_report
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    settings.vault_path.mkdir(parents=True, exist_ok=True)
    initialize_database(settings.database_path)

    pdfs = sorted(settings.inbox_path.glob("*.pdf"))
    report(f"Found {len(pdfs)} PDF{'s' if len(pdfs) != 1 else ''} in {settings.inbox_path}")
    if not pdfs:
        return []

    runtime_control, summarizer = _prepare_runtime(settings, report, verbose)
    return _ingest_batch(
        pdfs, settings, crossref, summarizer, report, runtime_control, verbose
    )

