import json
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
import typer

from mynd.ai.models import build_text_generator
from mynd.ai.runtime import RuntimeControl
from mynd.ai.topics import TopicWriter
from mynd.clean import clean_vault
from mynd.connect import connect_vault
from mynd.connect.review import review_topic
from mynd.core import ingest_inbox
from mynd.courses.conversion import powerpoint_available
from mynd.courses.pipeline import ingest_slides, review_saved_slides
from mynd.courses.review import review_assignment
from mynd.questionnize import QuestionWriter
from mynd.questionnize.service import questionnize_note, resolve_note_path
from mynd.resolution.crossref import CrossrefClient
from mynd.settings import Settings, load_settings
from mynd.vault.review_queue import write_review_queue
from mynd.vault.safety import maintenance_lock, restore_run
from mynd.years import organize_years

app = typer.Typer(help="Local-first research library for Obsidian.")


@app.command("years")
def years(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
    apply: bool = typer.Option(False, "--apply", help="Create/move year hubs and migrate publication links"),
    json_output: bool = typer.Option(False, "--json", help="Print the complete migration report"),
) -> None:
    """Preview publication-year organization under Years/<decade>/year-YYYY.md."""
    settings = settings_from(config)
    try:
        result = organize_years(settings, apply=apply)
    except (OSError, ValueError, TypeError) as exc:
        typer.echo(f"Could not organize years: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False))
    else:
        action = "Applied" if apply else "Preview"
        create, move, update = ("created", "moved", "updated") if apply else ("to create", "to move", "to update")
        typer.echo(f"{action}: {len(result['years'])} publication years, "
                   f"{len(result['created'])} hubs {create}, {len(result['moved'])} {move}, "
                   f"{len(result['updated'])} notes {update}.")
        if result["corrected_years"]:
            typer.echo(f"Evidence-backed date corrections: {len(result['corrected_years'])} (details with --json)")
        for issue in result["issues"]:
            typer.echo(f"{issue['path']}: {issue['error']}", err=True)
        if result["run_id"]:
            typer.echo(f"Backup run: {result['run_id']} (restore with mynd clean --restore ID)")
        if not apply:
            typer.echo("No files changed. Use --apply to perform this migration.")
    if result["status"] == "partial":
        raise typer.Exit(code=1)


@app.command()
def clean(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
    apply: bool = typer.Option(False, "--apply", help="Apply unambiguous mechanical repairs"),
    all_notes: bool = typer.Option(False, "--all", help="Bypass cached diagnostics"),
    json_output: bool = typer.Option(False, "--json", help="Print the full machine-readable report"),
    restore: str | None = typer.Option(None, "--restore", help="Restore a clean/connect run by its ID"),
) -> None:
    """Inspect Markdown/link defects; never rewrite prose or guess missing targets."""
    settings = settings_from(config)
    try:
        if restore:
            if apply or all_notes:
                raise ValueError("--restore cannot be combined with --apply or --all")
            with maintenance_lock(settings):
                paths = restore_run(settings, restore)
            typer.echo(json.dumps({"restored": paths}) if json_output
                       else f"Restored {len(paths)} files from {restore}")
            return
        result = clean_vault(settings, apply=apply, all_notes=all_notes)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        typer.echo(f"Could not clean vault: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False))
    else:
        typer.echo(f"Clean: {result['checked']} checked, {result['cached']} cached, "
                   f"{result['changed']} files repaired.")
        counts = Counter(item["status"] for note in result["notes"] for item in note.get("findings", []))
        typer.echo("Findings: " + (", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"))
        year_links = sum(item["rule"] == "unresolved-year-link" for note in result["notes"]
                         for item in note.get("findings", []))
        if year_links:
            typer.echo(f"{year_links} links refer to missing publication-year hubs. "
                       "Run 'mynd years' to preview their organization, then 'mynd years --apply'.")
        displayed = 0
        for note in result["notes"]:
            if note.get("error"):
                typer.echo(f"{note['path']}: skipped: {note['error']}")
            for item in note.get("findings", []):
                if item["rule"] != "unresolved-year-link" and displayed < 20:
                    typer.echo(f"{note['path']}:{item['line']}: {item['status']}: {item['message']}")
                    displayed += 1
        if sum(counts.values()) > displayed:
            typer.echo("Use --json for every finding, including unresolved note/year links.")
        if result["run_id"]:
            typer.echo(f"Backup run: {result['run_id']} (restore with mynd clean --restore ID)")
    if any(note.get("status") == "skipped" or any(
        item["status"] in {"needs_review", "fixable"} for item in note.get("findings", [])
    ) for note in result["notes"]):
        raise typer.Exit(code=1)


@app.command("connect")
def connect_topics(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
    all_notes: bool = typer.Option(False, "--all", help="Reanalyze all substantive notes"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without vault or maintenance-state writes"),
    review: bool = typer.Option(False, "--review", help="Also reopen rejected proposals"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Leave all new topics pending"),
    web: bool | None = typer.Option(None, "--web/--offline", help="Override configured web enrichment"),
    refresh_web: bool = typer.Option(False, "--refresh-web", help="Refresh cached web sources"),
    mode: str | None = typer.Option(None, "--mode", help="Initial inference mode: normal or silent"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show Ollama options, timings, token rates and runtime allocation"),
    json_output: bool = typer.Option(False, "--json", help="Print the full machine-readable report"),
    reclassify: list[str] | None = typer.Option(None, "--reclassify", help="Correct saved CANDIDATE_ID=BREADTH; repeatable"),
) -> None:
    """Discover topic hubs; review new topics, then update approved connections."""
    settings = settings_from(config)
    runtime = RuntimeControl(settings.mode_file)
    if mode:
        if dry_run:
            raise typer.BadParameter("For --dry-run, set the profile beforehand with 'mynd mode silent'")
        try:
            runtime.set_mode(mode)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if refresh_web and web is False:
        raise typer.BadParameter("--refresh-web cannot be combined with --offline")
    if refresh_web:
        web = True
    progress = lambda message: typer.echo(message, err=True)
    generator = build_text_generator(runtime_control=runtime, progress=progress, verbose=verbose)
    reviewer = review_topic if not non_interactive and sys.stdin.isatty() else None
    try:
        breadth_overrides = {}
        for value in reclassify or []:
            identifier, separator, breadth = value.partition("=")
            if not separator or not identifier or not breadth:
                raise ValueError("Use --reclassify CANDIDATE_ID=umbrella|established|specialized|niche")
            breadth_overrides[identifier] = breadth
        result = connect_vault(
            settings, TopicWriter(generator, progress), reviewer=reviewer, all_notes=all_notes,
            dry_run=dry_run, reopen=review, web_enabled=web, refresh_web=refresh_web,
            breadth_overrides=breadth_overrides,
        )
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error, httpx.HTTPError) as exc:
        typer.echo(f"Could not connect vault: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        generator.close()
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False))
    else:
        action = "would change" if dry_run else "changed"
        typer.echo(f"Connect: {result['analyzed']} analyzed, {result['cached']} cached, "
                   f"{len(result['topics'])} approved topics, {result['changed']} files {action}.")
        grouping = result["grouping"]
        typer.echo(f"Grouping: {grouping['completed_batches']}/{grouping['batches']} batches complete, "
                   f"{grouping['cached_batches']} cached, {grouping['groups']} groups, "
                   f"{grouping['unresolved_candidates']} candidates retained unmerged for retry/review.")
        for proposal in result["pending"]:
            typer.echo(f"Pending review: {proposal['title']} — {proposal['breadth']}, "
                       f"{proposal['source_count']}/{proposal['minimum_sources']} original sources")
        register = result["candidate_register"]
        typer.echo("Candidate register" + (" (preview only)" if dry_run else "") + ": " + register["path"])
        typer.echo("Candidates: " + ", ".join(f"{key}={value}" for key, value in register["counts"].items()))
        if result["legacy_approvals"]:
            typer.echo(f"{len(result['legacy_approvals'])} legacy approvals require new-policy review; "
                       "they will not create nodes automatically.")
        for error in result["errors"]:
            typer.echo(f"{error['stage']}: {error.get('path', error.get('topic_id', error.get('batch', 'vault')))}: "
                       + error["error"], err=True)
        if result["run_id"]:
            typer.echo(f"Backup run: {result['run_id']} (restore with mynd clean --restore ID)")
    if result["status"] in {"partial", "pending_review"}:
        raise typer.Exit(code=1)


def _grobid_health_url(url: str) -> str:
    return f"{url.rstrip('/')}/api/isalive"


def _wait_for_grobid(url: str, timeout: float = 60.0, interval: float = 1.0) -> bool:
    """Poll GROBID until its health endpoint answers or the window expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with httpx.Client() as client:
                client.get(_grobid_health_url(url), timeout=2.0).raise_for_status()
            return True
        except (httpx.HTTPError, OSError):
            time.sleep(interval)
    return False


def settings_from(config: Path) -> Settings:
    if not config.exists():
        raise typer.BadParameter(f"Config file not found: {config}")
    try:
        return load_settings(config)
    except (OSError, ValueError, TypeError) as exc:
        raise typer.BadParameter(f"Invalid config {config}: {exc}") from exc


def _slide_reviewer(settings: Settings, plain: bool = False):
    def reviewer(source, proposal, known):
        try:
            return review_assignment(source, proposal, known, settings.slides_library, plain=plain)
        except typer.Abort as exc:
            # Cancellation is a user interrupt, not a recoverable per-file failure.
            raise KeyboardInterrupt from exc
    return reviewer


@app.command()
def ingest(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c", help="Path to config.toml"),
    mode: str | None = typer.Option(None, "--mode", help="Initial inference mode: normal or silent"),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show course stages, cache/save paths, failure tracebacks, and Ollama diagnostics",
    ),
    kind: str = typer.Option("all", "--kind", help="Process all, papers, or slides"),
    regenerate: bool = typer.Option(False, "--regenerate", help="Regenerate slide study material"),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Leave unconfirmed slide assignments pending review",
    ),
    review: bool = typer.Option(False, "--review", help="Reopen saved slide assignments too"),
    plain_review: bool = typer.Option(False, "--plain-review", help="Use the numbered text form"),
) -> None:
    """Ingest paper PDFs and reviewed course slides from their separate inboxes."""
    settings = settings_from(config)
    if kind not in {"all", "papers", "slides"}:
        raise typer.BadParameter("--kind must be all, papers, or slides")
    if review and (kind == "papers" or non_interactive or not sys.stdin.isatty()):
        raise typer.BadParameter("--review requires slides and an interactive terminal")
    if mode is not None:
        try:
            RuntimeControl(settings.mode_file).set_mode(mode)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    results = []
    if kind in {"all", "papers"}:
        client = CrossrefClient(settings.crossref_email)
        try:
            results.extend(ingest_inbox(settings, client, verbose=verbose))
        finally:
            client.close()
    slides_results = []
    if kind in {"all", "slides"}:
        reviewer = None
        if not non_interactive and sys.stdin.isatty():
            reviewer = _slide_reviewer(settings, plain_review)

        slides_results = ingest_slides(settings, reviewer, verbose=verbose, regenerate=regenerate,
                                      review_existing=review)
        results.extend(slides_results)
    for result in results:
        typer.echo(json.dumps(result, ensure_ascii=False, default=str))
    if not results:
        typer.echo("No input files or pending course updates found in the selected inboxes")
    if any(item["status"] in {"failed", "partial", "pending_review", "course_partial"}
           for item in slides_results):
        raise typer.Exit(code=1)


@app.command("review-slides")
def review_slides(
    target: str | None = typer.Argument(None, help="Saved lecture ID (omit to review all)"),
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
    plain_review: bool = typer.Option(False, "--plain-review", help="Use the numbered text form"),
) -> None:
    """Correct saved slide metadata without model calls, even after removing inbox files."""
    settings = settings_from(config)
    if not sys.stdin.isatty() and not plain_review:
        raise typer.BadParameter("Review needs a terminal; use --plain-review for the text form")
    try:
        results = review_saved_slides(settings, _slide_reviewer(settings, plain_review), target)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    for result in results:
        typer.echo(json.dumps(result, ensure_ascii=False, default=str))
    if any(result["status"] == "metadata_updated" for result in results):
        typer.echo("Metadata saved. Run 'mynd ingest --kind slides' to regenerate pending study "
                   "material. Keep or restore the source in the slides inbox for lecture generation.",
                   err=True)
    if not results:
        typer.echo("No saved lectures to review")


@app.command("mode")
def set_mode(
    mode: str = typer.Argument(..., help="Inference mode: normal or silent"),
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """Change inference mode for a running or future ingestion job."""
    settings = settings_from(config)
    control = RuntimeControl(settings.mode_file)
    try:
        control.set_mode(mode)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Inference mode set to {control.mode()}; running jobs apply it at the next model request.")


@app.command()
def review_queue(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """Generate a Markdown queue for citations needing review."""
    settings = settings_from(config)
    path = write_review_queue(settings)
    typer.echo(str(path))


@app.command()
def questionnize(
    target: str = typer.Argument(..., help="Paper note path, paper ID, or DOI"),
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """Generate ten study questions from an ingested paper's summary."""
    settings = settings_from(config)
    try:
        note_path = resolve_note_path(settings, target)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    generator = build_text_generator(
        runtime_control=RuntimeControl(settings.mode_file),
        progress=lambda message: typer.echo(message, err=True),
    )
    try:
        if not generator.ensure_model():
            typer.echo(f"Model {generator.model} is unavailable.", err=True)
            raise typer.Exit(code=1)
        questions = questionnize_note(
            note_path,
            QuestionWriter(generator),
            progress=lambda message: typer.echo(message, err=True),
        )
    except (OSError, ValueError) as exc:
        typer.echo(f"Could not generate questions: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        generator.close()
    typer.echo(f"Wrote {questions.total} questions to {note_path}")


@app.command()
def doctor(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
    start_grobid: bool = typer.Option(False, "--start-grobid", help="Attempt to start GROBID via Docker if unavailable"),
) -> None:
    """Create directories and check basic configuration."""
    settings = settings_from(config)
    for path in (settings.inbox_path, settings.pdf_store_path, settings.vault_path,
                 settings.slides_inbox, settings.slides_library):
        path.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Vault: {settings.vault_path}")
    typer.echo(f"Inbox: {settings.inbox_path}")
    typer.echo(f"PDF store: {settings.pdf_store_path}")
    typer.echo(f"Database: {settings.database_path}")
    typer.echo(f"Slides inbox: {settings.slides_inbox}")
    typer.echo(f"Slides library: {settings.slides_library}")
    typer.echo("PowerPoint PDF conversion: " + (
        "Available (Automation permission may be required)" if powerpoint_available()
        else "Unavailable; PPTX requires PowerPoint on macOS, or manual PDF export"
    ))
    if start_grobid and settings.use_grobid:
        docker = shutil.which("docker")
        if docker is None:
            typer.echo("Error: Docker is not installed or not on PATH.")
        else:
            typer.echo("Attempting to start GROBID via Docker...")
            try:
                subprocess.run(
                    [
                        docker,
                        "run",
                        "-d",
                        "--rm",
                        "-p",
                        "8070:8070",
                        "grobid/grobid:0.9.1-crf",
                    ],
                    check=True,
                    timeout=30,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                typer.echo(
                    "Error: Docker command failed. Please ensure Docker/OrbStack is running."
                )
            else:
                typer.echo("Waiting for GROBID to become ready...")
                if not _wait_for_grobid(settings.grobid_url):
                    typer.echo("GROBID did not become ready within 60 seconds.")
    grobid_status = "Disabled"
    if settings.use_grobid:
        try:
            with httpx.Client() as client:
                client.get(_grobid_health_url(settings.grobid_url), timeout=2.0)
            grobid_status = "Connected"
        except (httpx.HTTPError, OSError) as exc:
            grobid_status = f"Unavailable ({exc})"
    typer.echo(f"GROBID enabled: {settings.use_grobid} ({settings.grobid_url}) - {grobid_status}")
    typer.echo("Directories are ready.")
