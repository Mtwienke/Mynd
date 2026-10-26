# Wisdom Storage v0.1

A local-first Python prototype that ingests PDFs from an inbox, stores metadata in SQLite,
and writes readable paper notes into an Obsidian vault.

## Requirements

- Python 3.11+
- Internet access for Crossref metadata lookups
- Optional: a local GROBID service for bibliography extraction

## Install

From this directory:

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1

python -m pip install -e ".[dev]"
```

Edit `config.toml` and set `vault_path` to your actual Obsidian vault directory.
Paths in `config.toml` are resolved relative to that file when they are not absolute.

Then run:

```bash
mynd doctor
pytest
```

Copy PDFs into `data/inbox/`, then run:

```bash
mynd ingest
mynd review-queue
```

Open the configured vault in Obsidian. Notes are written under `10 Papers/`.

## Inference modes and thermal response

Choose a reduced-resource profile when starting ingestion:

```bash
mynd ingest --mode silent
```

While ingestion is running, change its mode from another terminal:

```bash
mynd mode silent
mynd mode normal
```

Mode changes take effect on the next model request. Resource profiles and thermal control are active only in `silent` mode. On macOS, Mynd pauses model generation at `serious` or `critical` and resumes when the state drops below `serious`. When [`macmon`](https://github.com/vladkens/macmon) is installed, any fan RPM above 0 selects the extra-low profile for the next silent-mode request. Install macmon with `brew install macmon`; use `--verbose` to see CPU/GPU temperatures and fan RPM after model requests and PDFs. On other operating systems, manual modes work but automatic thermal response is not implemented.

Use `mynd ingest --verbose` in either mode to see Ollama request options, generation timing/token statistics, and model allocation changes reported by Ollama. Verbose diagnostics report runtime information without enabling the silent profile.

## Optional bibliography extraction with GROBID

GROBID is not bundled with this project. If Docker is installed, start a local instance:

```bash
docker run --rm --init -p 8070:8070 lfoppiano/grobid:0.9.1
```

Keep that process running, then ingest PDFs. If GROBID is unavailable, the PDF and note still ingest; references will not be extracted in that run.

## Current limitations

- The prototype finds a DOI in extracted PDF text and enriches metadata via Crossref.
- Citation extraction requires GROBID; reference resolution is strongest when a DOI is present.
- References to papers not yet in the vault are marked `external`, not automatically downloaded.
- The first version does not generate AI summaries or quizzes.
- Scanned PDFs may require OCR.
- PDF processing and API responses should be tested against your own paper collection before relying on the generated graph.

## Privacy

PDFs and SQLite data are stored locally. Crossref receives DOI lookup requests. When GROBID is run locally, the PDF is sent to that local service. This version does not send paper text to an LLM.
