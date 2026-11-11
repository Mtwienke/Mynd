import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import typer

from mynd.ai.models import build_text_generator
from mynd.ai.runtime import RuntimeControl
from mynd.core import ingest_inbox
from mynd.courses.conversion import powerpoint_available
from mynd.courses.pipeline import ingest_slides, review_saved_slides
from mynd.courses.review import review_assignment
from mynd.questionnize import QuestionWriter
from mynd.questionnize.service import questionnize_note, resolve_note_path
from mynd.resolution.crossref import CrossrefClient
from mynd.settings import Settings, load_settings
from mynd.vault.review_queue import write_review_queue

app = typer.Typer(help="Local-first research library for Obsidian.")


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
