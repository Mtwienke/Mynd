from pathlib import Path
import json

import typer

from mynd.core import CrossrefClient, load_settings, process_inbox, write_review_queue

app = typer.Typer(help="Local-first research library for Obsidian.")


def settings_from(config: Path):
    if not config.exists():
        raise typer.BadParameter(f"Config file not found: {config}")
    return load_settings(config)


@app.command()
def ingest(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c", help="Path to config.toml"),
):
    """Process every PDF in the configured inbox."""
    settings = settings_from(config)
    client = CrossrefClient(settings.crossref_email)
    try:
        results = process_inbox(settings, client)
    finally:
        client.close()
    for result in results:
        typer.echo(json.dumps(result, ensure_ascii=False, default=str))
    if not results:
        typer.echo(f"No PDFs found in {settings.inbox_path}")


@app.command()
def review_queue(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
):
    """Generate a Markdown queue for citations needing review."""
    settings = settings_from(config)
    path = write_review_queue(settings)
    typer.echo(str(path))


@app.command()
def doctor(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
):
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
