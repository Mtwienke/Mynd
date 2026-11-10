# Mynd v0.1

A local-first Python research library that ingests PDFs from an inbox, stores
metadata in SQLite, and writes readable paper notes into an Obsidian vault.

<img src="scorecard.png" width="100%">

## Project layout

The installable code lives under `src/` and is split by responsibility:

| Package | Responsibility |
| --- | --- |
| `mynd` | CLI, ingestion coordinator, settings, shared contracts, file and identity primitives |
| `mynd.ingestion` | PDF text and GROBID bibliography extraction |
| `mynd.resolution` | DOI normalization, Crossref lookup, and reference resolution |
| `mynd.storage` | SQLite schema and row-level persistence helpers |
| `mynd.vault` | Obsidian note rendering and citation linking |
| `mynd.ai` | Local Ollama backend, runtime controls, and summarization |
| `mynd.questionnize` | Study questions generated from a paper's summary |
| `mynd.courses` | Reviewed university-slide ingestion and local PowerPoint conversion |

Every domain package is nested under the `mynd` distribution package, so a
normal install exposes only `mynd` as a top-level import.

## Requirements

- Python 3.11+
- Internet access for Crossref metadata lookups
- Ollama with a local text model (default `qwen3:8b`) for summaries; ingestion
  continues without a summary when Ollama or the model is unavailable
- Optional: a local GROBID service for bibliography extraction

## Install

From this directory:

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1

python -m pip install -c constraints.txt -e ".[dev]"
```

Edit `config.toml` and set `vault_path` to your actual Obsidian vault directory.
Paths in `config.toml` are resolved relative to that file when they are not absolute.

Then run:

```bash
mynd doctor
pytest
```

Copy PDFs into the configured inbox (`./vault/00 Inbox` by default), then run:

```bash
mynd ingest
mynd review-queue
```

Open the configured vault in Obsidian. Notes are written under `10 Papers/`.

## University courses and slides

`mynd ingest` also processes the separate slides inbox, configured by
`slides_inbox_path` (default: `<inbox_path>/Slides`, or `vault/00 Inbox/Slides/`
in the supplied configuration). Put lecture **PDFs or PPTX files** there; nested
folders are supported. Files are copied, never moved or deleted from the inbox.
PDFs in the ordinary paper inbox continue through the existing paper pipeline.

```bash
mynd doctor                         # Create/check both inboxes and the slides library
mynd ingest                         # Papers and slides
mynd ingest --kind slides           # Slides only; no Crossref/GROBID requests
mynd ingest --kind papers           # Existing paper-only workflow
mynd ingest --kind slides --mode silent --verbose
mynd ingest --kind slides --regenerate
```

### Course review

For each new deck, the local model proposes a lecture title, course membership,
language, and any explicitly established university, lecturer, semester, and
session number. It sees **slide content only**, never parent-folder names or file
timestamps. Known, human-confirmed course metadata is supplied as matching context.
Lecture titles and cited textbook/resource authors are not assumed to identify a
course or its instructor. Unknown information remains unknown.

Run in an interactive terminal to review the proposal and its quoted source-page
evidence. Select an existing course or create one, correct the lecture title,
language, and order, then confirm. If no session order is established in the slides,
the filename is offered **only as an ordering fallback**; natural sorting places
lecture 2 before lecture 10. Declining a proposal leaves the file pending.

`--non-interactive` (also automatic when stdin is not a terminal) **never approves
new assignments**. Pending proposals are reported as JSON, without writing a vault
note or assigning a course. Re-run interactively to confirm them. Confirmed
memberships are remembered, so unattended incremental runs can process updates to
known decks. If the model is unavailable, interactive manual assignment is still
possible, but study generation is recorded as incomplete and retried later.

### Notes, evidence, and updates

Each approved course gets a folder under the actual Obsidian vault:

```text
15 Slides/<course name> (<stable id>)/
├── Course overview.md
├── <lecture title> (<stable id>).md
└── Sources/
    ├── <original PDF or PPTX> (<source hash>).<extension>
    └── <converted PDF> (<source hash>).pdf
```

Every file becomes one lecture note, not one note per page. Lecture and course notes
link to each other. Lecture notes contain detailed exam-preparation sections in the
source language and ten answered questions: **three concepts/definitions, four
understanding/application, and three connections/synthesis**. The course overview
contains an ordered lecture index, a whole-course study map, and a ten-question
revision quiz. With multiple usable lectures, its synthesis questions must cite
at least two lectures. With only one lecture it explicitly discloses that limitation.
Mixed-language courses use the predominant lecture language for the overview.

Study sections and answers require verbatim supporting excerpts and valid lecture/
page references. The model selects supplied source IDs, which Mynd resolves to exact
extracted passages instead of asking the model to reproduce quotes. These are rendered as
native Obsidian PDF-page links. The local Ollama decoder is constrained by JSON
schemas containing the allowed source IDs. Lecture questions use retained slide evidence, not
only a short overview. Long decks are processed in bounded chunks; course synthesis
uses bounded hierarchical reduction rather than submitting a whole semester at once.
Course requests use an 8192-token model context while retaining the existing
normal/silent CPU, GPU, and thermal controls.
Announced syllabus topics are distinguished from material actually ingested.
Exact-quote validation checks source provenance, **not mathematical correctness or
whether every generated interpretation is logically supported**; verify important
claims yourself. Insufficient supported questions are reported rather than padded
with invented answers.

Unchanged, completed decks skip inference. Changed content at a known inbox path
is treated as a revision of that lecture; renamed/duplicate identical files reuse
the existing note. Keep filenames stable for revisions; if both a filename and its
content change, Mynd cannot automatically identify it as the same lecture. Only
affected course syntheses are updated. `--regenerate` explicitly refreshes slide
study material; it does not alter paper refresh behavior. Use that option rather
than the paper-specific `mynd questionnize` command for course notes. Failed/incomplete stages
are retried on later runs, and one broken deck does not abort the other decks.

Generated content lives in `<!-- mynd:... -->` managed blocks. Personal notes and
custom sections **outside those blocks** are preserved during updates.
Do not remove the markers: damaged/duplicated markers cause a safe refusal to
overwrite the note. Prior source revisions are retained for existing page links;
edited managed originals are never silently replaced. Removing an inbox file does
not delete its library note or archive. Incomplete summaries/quizzes and stale
previous generations are explicitly marked. Slide failures, incomplete generation,
and pending review cause a nonzero CLI exit status after JSON results are printed.
Check the proposed language during review, especially for mixed-language slides:
model-inferred metadata is tentative even when all cited source IDs are valid.

### Automatic PowerPoint conversion

PPTX ingestion currently requires **Microsoft PowerPoint on macOS**. Mynd opens only
a uniquely named temporary copy, exports a one-page-per-slide PDF, and closes only
that presentation; it never quits PowerPoint or saves changes to your source.
Conversion is cached by input content hash and the exported PDF's hash. The PDF's
page count must match the original slide count; a handout/notes layout or omitted
hidden slides is rejected rather than producing misleading page references.

On first use, complete any PowerPoint first-run/sign-in prompts and allow the
terminal/app running Mynd to automate PowerPoint under **System Settings → Privacy
& Security → Automation**. Office may also request file access. Exports have bounded
timeouts and actionable error reporting. `mynd doctor` checks installation, not
whether Office activation, file access, or PDF export actually works. Other systems
can ingest PDFs normally; export PPTX files manually there. No converter or OCR
package is automatically installed. Macro-bearing presentations are rejected.

Conversion does **not** OCR screenshots or interpret diagrams. The text-first
extractor preserves page numbers, normalizes recognized Symbol-font glyphs, and
flags sparse pages, images, and unresolved characters. Mathematical layout,
screenshots, and vector graphics may still be incomplete even without a warning.
The original slides remain the authority for equations, code, and derivations.

## Paper study questions

When a model is available, ingesting a new paper also writes an **Extracted
questions** chapter: three questions about the main idea, four about the
methodology, and three about specific findings. To regenerate those questions
for an already ingested paper, pass its note path, paper ID, or DOI:

```bash
mynd questionnize "Obsidian/MyndVault/10 Papers/A Study (abcdef123456).md"
mynd questionnize doi-abcdef123456
mynd questionnize 10.1234/study
```

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
- When the configured model is available, ingestion creates an AI summary,
  tentative reference annotations, and ten extracted study questions. If it is
  unavailable, ingestion continues and the note records that no summary was
  generated. Questions are based only on the generated summary, not the full
  paper text.
- Scanned PDFs may require OCR.
- PDF processing and API responses should be tested against your own paper collection before relying on the generated graph.

## Privacy

PDFs, extracted text, and SQLite data are stored locally. Crossref receives DOI lookup requests, and a locally run GROBID service receives the PDF for bibliography extraction. Extracted paper text (excluding a terminal bibliography) is sent to the configured Ollama model on your machine for summarization and reference annotation. Slide text and retained evidence are likewise sent only to local Ollama; course slides bypass Crossref and GROBID entirely. PPTX conversion is performed by the installed local PowerPoint application. Course state/text/conversion caches remain under `data_path`, SQLite course records use `database_path`, and managed slide sources live inside the Obsidian vault. Mynd has no hosted-model backend: summarization always runs locally, and unavailable models leave generation incomplete for a later retry.
