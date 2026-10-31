import json
from pathlib import Path

import typer

from mynd.ai.runtime import RuntimeControl
from mynd.core import ingest_inbox
from mynd.resolution.crossref import CrossrefClient
from mynd.settings import Settings, load_settings
from mynd.vault.review_queue import write_review_queue

app = typer.Typer(help="Local-first research library for Obsidian.")


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
def doctor(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """Create directories and check basic configuration."""
    settings = settings_from(config)
    for path in (settings.inbox_path, settings.pdf_store_path, settings.vault_path):
        path.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Vault: {settings.vault_path}")
    typer.echo(f"Inbox: {settings.inbox_path}")
    typer.echo(f"PDF store: {settings.pdf_store_path}")
    typer.echo(f"Database: {settings.database_path}")
    typer.echo(f"GROBID enabled: {settings.use_grobid} ({settings.grobid_url})")
    typer.echo("Directories are ready. GROBID connectivity is not checked by this command.")
