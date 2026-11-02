import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import typer

from mynd.ai.models import build_text_generator
from mynd.ai.runtime import RuntimeControl
from mynd.core import ingest_inbox
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


@app.command()
def ingest(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c", help="Path to config.toml"),
    mode: str | None = typer.Option(None, "--mode", help="Initial inference mode: normal or silent"),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show Ollama request, allocation, and generation timing diagnostics",
    ),
) -> None:
    """Process every PDF in the configured inbox."""
    settings = settings_from(config)
    if mode is not None:
        try:
            RuntimeControl(settings.mode_file).set_mode(mode)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    client = CrossrefClient(settings.crossref_email)
    try:
        results = ingest_inbox(settings, client, verbose=verbose)
    finally:
        client.close()
    for result in results:
        typer.echo(json.dumps(result, ensure_ascii=False, default=str))
    if not results:
        typer.echo(f"No PDFs found in {settings.inbox_path}")


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
    for path in (settings.inbox_path, settings.pdf_store_path, settings.vault_path):
        path.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Vault: {settings.vault_path}")
    typer.echo(f"Inbox: {settings.inbox_path}")
    typer.echo(f"PDF store: {settings.pdf_store_path}")
    typer.echo(f"Database: {settings.database_path}")
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
