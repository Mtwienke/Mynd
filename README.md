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
| `mynd.clean` | Incremental Markdown/link inspection and conservative mechanical repairs |
| `mynd.connect` | Reviewed topic discovery, connections, and optional web enrichment |
| `mynd.years` | Reversible publication-year organization and link migration |

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
mynd ingest --kind slides --review   # Reopen saved assignments too, then generate as needed
mynd review-slides                  # Edit all saved metadata, without model calls
mynd review-slides lecture-<id>      # Edit one saved lecture, even if absent from the inbox
```

### Course review

For each new deck, the local model proposes a lecture title, course membership,
language, and any explicitly established university, lecturer, semester, and
session number. It sees **slide content only**, never parent-folder names or file
timestamps. Known, human-confirmed course metadata is supplied as matching context.
Lecture titles and cited textbook/resource authors are not assumed to identify a
course or its instructor. Unknown information remains unknown.

Interactive review opens a **prefilled terminal form** showing all metadata and
read-only source-page evidence together. Use **Tab / Shift-Tab** to navigate;
fields scroll into view on smaller terminals. Edit course name, university,
semester, lecturer, course output language, lecture title, lecture language, and
lecture order. **Change course** switches membership to an existing or new course;
editing **Course name** renames the selected course instead. Course fields are
shared by **all** lectures in that course; lecture fields affect only this deck.
If no session order is established, the filename is offered **only as an ordering
fallback**; natural sorting places lecture 2 before lecture 10.

**Save / Ctrl-S** opens one final confirmation of every field. **Edit again**
returns to the same draft without losing changes. **Skip file** explicitly leaves
the assignment untouched, reports `skipped`, and does not count as an error.
**Cancel run / Ctrl-C** aborts the run without saving the current draft (earlier
confirmed files stay saved). No assignment or vault note is written before final
confirmation. `--plain-review` provides the same behavior as a numbered text form:
choose a field to edit, `course` to switch membership, or `save`, `skip`, `cancel`.
The text form also supports unique existing course names and disambiguates by number.

Saved choices are in **SQLite**, not in the Markdown notes. Deleting an Obsidian
note only causes it to be recreated; it is not a metadata reset. Use `--review`
to reopen even unchanged inbox files, prefilled with **saved human choices** rather
than old model guesses. Or use `mynd review-slides [lecture-id]` for metadata-only
editing without extraction, PowerPoint, or model calls; it also covers lectures
whose inbox files were removed. Then run ingestion to regenerate affected content
(lecture generation needs the source present in the slides inbox).

Language/title/membership corrections invalidate the affected lecture summary and
questions; course metadata/membership changes invalidate affected course syntheses
and quizzes. When correcting a mistaken language, check **both** lecture language
and course output language. Other lectures retain their own language and study
material. Stable IDs, filenames, note/archive paths, backlinks, and personal notes
are retained—even if a course rename or reassignment leaves an older folder name.
Untouched generated title headings and managed metadata/links show the corrected
names; customized headings are preserved.

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
Course output language is editable independently; when unspecified, Mynd uses the
predominant lecture language for the overview.

Study sections and answers require verbatim supporting excerpts and valid lecture/
page references. The model selects supplied source IDs, which Mynd resolves to exact
extracted passages instead of asking the model to reproduce quotes. These are rendered as
native Obsidian PDF-page links. The local Ollama decoder is constrained by JSON
schemas containing the allowed source IDs. Lecture questions use retained slide evidence, not
only a short overview. Long decks are processed in bounded chunks; course synthesis
uses bounded hierarchical reduction rather than submitting a whole semester at once.
Reduction carries compact source references, retaining exact evidence quotes outside
the prompt and restoring them for final citations and quiz generation. Each multi-batch
step must shrink its input; non-condensing output gets one repair attempt and then
stops with a useful error, rather than repeatedly processing nearly identical levels.
Verbose logs report input/output material sizes at every successful level.
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
Metadata renames only update a title heading outside those blocks when it still
exactly matches Mynd's original generated header; customized headers are left alone.
Do not remove the markers: damaged/duplicated markers cause a safe refusal to
overwrite the note. Prior source revisions are retained for existing page links;
edited managed originals are never silently replaced. Removing an inbox file does
not delete its library note or archive. Incomplete summaries/quizzes and stale
previous generations are explicitly marked. Slide failures, incomplete generation,
and pending review cause a nonzero CLI exit status after JSON results are printed.
Check the proposed language during review, especially for mixed-language slides:
model-inferred metadata is tentative even when all cited source IDs are valid.

### Debugging ingestion

```bash
mynd ingest --kind slides --verbose
```

`--verbose` adds stage start/completion timings, working-cache hits/misses,
extraction counts and warnings, model request/response lengths, validation/repair
attempts, question counts, archive destinations, SQLite save steps, and Obsidian
note paths. Failed stages include exception tracebacks. PowerPoint diagnostics
include process/AppleEvent timeouts and the last export-stage marker returned by
the application; its captured stderr is shown on completion or timeout.

Diagnostics go to **stderr**, leaving final JSON results on stdout. Full model
prompts, raw responses, and slide bodies are not dumped by debug logging (the
interactive review still displays its supporting source excerpts). Failures show
their reason immediately and include a `stage` field in final JSON even without
`--verbose`, making a review failure distinguishable from a conversion, model,
database-save, or note-writing failure.

### Automatic PowerPoint conversion

PPTX ingestion currently requires **Microsoft PowerPoint on macOS**. Mynd opens only
a uniquely named temporary copy, exports a one-page-per-slide PDF, and closes only
that presentation; it never quits PowerPoint or saves changes to your source.
The save command uses a file-specifier destination, since passing an AppleScript
alias can silently leave an empty PDF in some PowerPoint versions.
The PDF is validated and saved **before** presentation cleanup. Cleanup uses a
bounded numeric presentation lookup in a separate process (5-second AppleEvent /
10-second process timeout), comparing real file aliases before closing anything
(including macOS `/var` versus `/private/var` path aliases).
If cleanup fails, a warning identifies the temporary presentation to close manually;
an already validated PDF remains available and ingestion continues. Empty,
incomplete, or wrong-page-count exports are still rejected.
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

## Publication years and chronology

Papers and citation placeholders link to real publication-year notes, organized
under a **`Years/`** folder:

```text
Years/
├── 80s/year-1988.md
├── 90s/year-1995.md
├── 00s/year-2003.md
├── 10s/year-2016.md
└── 20s/year-2023.md
```

Notes display a short year through an explicit, vault-relative link:
`[[Years/20s/year-2023|2023]]`. One hub is created per distinct known publication
year, not per paper. New hubs have `type: year`, a numeric `year` and `decade`, and
the tag `mynd/year`. Existing year-note content and custom metadata are preserved.
Ingestion automatically creates missing hubs for both paper dates and reference
dates; it never regenerates or overwrites your personal chronology in a year note.
Course semester labels and ingestion timestamps are not assumed to be publication years.

To organize an existing vault:

```bash
mynd years                    # Read-only preview; no model, network, or state writes
mynd years --json             # Full preview, including proposed date corrections
mynd years --apply            # Create/move year notes and migrate their links
```

This migrates the old `[[2023]]` links, links plain publication-year metadata lines,
and moves unambiguous existing year notes to the appropriate folder. Wiki/Markdown
destinations, quoted YAML property links, and Canvas file-card paths follow moved
notes; outgoing relative Markdown links in a moved year note are rebased. Labels,
anchors, Markdown link titles, meaningful whitespace, source dumps, and personal
sections remain intact. Conflicting duplicate year notes or occupied destinations
cause a safe refusal rather than a merge/overwrite. Dotfolders, symlinks, and configured
maintenance exclusions remain protected. Rerunning a completed migration is a no-op.

Abbreviations are fixed and century-safe: decades **1950–2049** use `50s` through
`40s`; all other decades use their full name, e.g. `1890s` and `1910s`. This prevents
1911 and 2011 from being mixed into one `10s` folder and avoids date-dependent
folder renaming as time passes.

Reference-date extraction ignores DOI/arXiv identifiers, URLs, and labeled issue/
volume/page numbers. Migration corrects a previous identifier-as-year mistake only
when the retained source citation establishes a replacement and the note still
matches the old generated metadata/field pair. Raw GROBID evidence is not altered;
manual date overrides survive. Missing or ambiguous dates are not guessed, and
implausible future dates are reported rather than turned into year hubs.

In Obsidian Graph view, hide the chronology hubs using **Search files**:

```text
-path:"Years/"
```

Alternatively use `-file:year-` or `-tag:#mynd/year`. To color chronology nodes,
create a graph Group using `path:"Years/"`. Year hubs expose chronological clusters,
but Obsidian's force-directed graph does not enforce a chronological axis.
Year/decade nodes are excluded from AI topic discovery.

Year migrations use the same snapshots and recovery journal as cleanup/connection
runs. The printed backup ID can be restored with `mynd clean --restore RUN_ID`,
including moves, provided none of the affected files were edited afterwards.

## Vault cleanup and topic connections

These are **independent commands**. Ingestion does not automatically run either
one; their service functions can be used by a future ingestion coordinator.
Run them from the project directory, or pass `--config /path/to/config.toml`.

### Clean

```bash
mynd clean                     # Inspect changed notes; no note changes
mynd clean --all               # Recheck everything, bypassing cached diagnostics
mynd clean --apply             # Apply only unambiguous mechanical repairs
mynd clean --json              # Full report, including unresolved links
```

Cleanup inspects every non-excluded Markdown file, including citation
placeholders. It repairs known escaped paper-summary headings, recognizable
imported JATS abstracts, and uniquely identifiable stale/case-mismatched link
destinations. Link labels, optional Markdown link titles, meaningful whitespace,
personal text, code, math, and existing note names are retained. Paper generation
now renders actual nested summary headings rather than literal `\## Overview`.

Ambiguous targets, missing headings/blocks, malformed YAML/links/fences,
suspicious emphasis/table formatting, and damaged ownership markers are reported,
**not guessed or rewritten**. Missing year notes such as `[[2023]]` are unresolved
links, not syntax errors; cleanup never creates or deletes notes to satisfy them.
Use `mynd years --apply` to migrate these links to real, filterable year hubs.
Capitalization-only mismatches are distinguished from missing files. Links in
checksummed generated sections are updated by `connect`, not patched behind its back.

Diagnostics are cached against file contents, parser version, and the vault's
link/heading/block inventory. Target renames and heading changes invalidate inbound
diagnostics even when the referring note did not change. An exit code of **1**
indicates skipped files, review findings, or unapplied repairs; unresolved links
alone do not fail the command.

### Connect

```bash
mynd connect --offline                 # Discover, review, synthesize, and link locally
mynd connect --mode silent --verbose   # Existing local inference/thermal controls
mynd connect --non-interactive --json  # New topics remain pending; approved topics may update
mynd connect --dry-run --offline       # Preview without note or maintenance-state writes
mynd connect --review                  # Also reconsider previously rejected proposals
mynd connect --all                     # Force full rediscovery and mention verification
mynd connect --verbose                 # Or -v: Ollama/runtime diagnostics in the current topic context
```

### Connect progress and diagnostics

Normal output identifies the current phase, **percentage of completed units within
that phase**, and the note/topic being processed. The eight phases are scanning,
discovery, grouping, breadth/evidence assessment, review, topic updates, source-note
connections, and journaled writes. For example:

```text
[Connect 2/8 | Discovery 35% (5/14 notes)] 10 Papers/Example.md — Discover topics [passage batch 1/3]: local model request 1/2
[Connect 4/8 | Candidate evidence 37% (64/172 topics)] LoRA — Verify LoRA: 3 passages from 3 note(s): ...
[Connect 6/8 | Update topics 25% (1/4 topics)] Large Language Models → 20 Topics/... — Synthesizing vault topic from 7 supporting passage(s)
```

These are **work counters, not elapsed-time/ETA or model-token percentages**: one
model request may take much longer than a cached item. The in-flight item does not
advance the completed count until it finishes. Assessment and broader-topic passes
have separate, explicitly named counters because their totals become known later.
Failed/deferred items count as processed, not successful; their retry/hold status is
printed and remains in the final report. Interrupted phases do not claim completion.

Grouping shows candidate titles and split/retry context. Evidence checks identify
the topic and input notes; synthesis identifies reduction passes and batches.
Cache reuse, eligibility counts, legacy holds, review pauses, web research, and
unchanged output are distinguished. Fast scanning/linking progress is throttled
to avoid printing a line for every skipped file. Write progress advances only
after each changed file has a durable recovery-journal checkpoint. Dry runs
explicitly report previews rather than claiming to have written files.

`mynd connect --verbose` (or `-v`) adds the same local-model diagnostics as ingestion:
Ollama options, timings, token counts/rates, runtime allocation, and applicable
thermal diagnostics. These messages inherit the current note/topic context.
Prompts and source bodies are not logged by progress/diagnostics. Progress goes to
**stderr**, so `--json` stdout remains machine-readable. Changes to progress output
apply to the next invocation, not a process that is already running.

The first run examines substantive paper, lecture, and handwritten Markdown notes.
Citation placeholders, bibliographies, template text, extracted questions, and
generated topic sections are not discovery evidence. Course overviews can receive
connections, but do not count as independent supporting notes. Original PDFs and
unrendered slide text are **not** analyzed by this command.

New nodes are breadth-gated, not selected merely because the model calls a
single-note concept important:

| Breadth | Minimum original sources | Calibration examples |
| --- | ---: | --- |
| `umbrella` | **1** | Artificial Intelligence, Evolutionary Algorithms, Large Language Models |
| `established` | **2** | Reinforcement Learning, CNNs, PINNs, Self-attention |
| `specialized` | **3** | LoRA, AdamW, particular optimization techniques |
| `niche` | **5** | Paper-specific architectures and narrowly defined variants |

Only **substantive discussion** counts: explanation, application, comparison, or
meaningful discussion—not a passing name-drop. Eligible candidates are contextually
verified against actual passages **before** reaching review. The source threshold
is checked again before creating a topic file. Importance never bypasses these
hard floors. Named model families (GPT, Llama, BERT, etc.), datasets, benchmarks,
people, publication titles, venues, years, generic headings, and other metadata are
excluded regardless of source count. General architectures/methods, tools/software,
and paper-specific inventions remain eligible at the appropriate breadth.

Count each original paper, lecture, or personal note once. Multiple passages,
summaries, and excerpts of one work do not add independent support. DOI identity is
case-insensitive; generated paper/lecture notes use their stable work IDs. For
handwritten derived notes, declare `source_work_id: "paper-..."` or
`derived_from: "[[Path/Original note]]"` in frontmatter. Multiple derivatives can
then link normally but count as one original. Multi-source summaries without
passage-level attribution and course overviews add no independent support. Mynd
does not guess that an arbitrary citation makes a personal note a derivative.

The local model may suggest a nearest broader concept for a narrow candidate.
That relationship alone is **not** evidence: the source must substantively support
the broader topic in context. For example, LoRA can remain a candidate while the
source connects to an approved Large Language Models node. Broader concepts are
not synonyms/aliases of their narrow children. Both broad and specific connections
are retained when substantively supported; no automatic parent hierarchy is added.

Only eligible topics require terminal review: approve, edit their English title/
definition/aliases/public search query, correct their **[b]readth**, merge into a
policy-approved topic, reject, defer, or explicitly adopt a handwritten topic note.
The classification explanation, original-source count/threshold, and supporting
excerpts are shown before approval. Editing a title/definition rechecks entity type
and evidence; adoption cannot bypass the floor. Adoption preserves existing text
and appends a generated section. Noninteractive runs never approve new topics and
return **1** while eligible proposals are pending. Rejections are remembered until
evidence changes or `--review` reopens them.

### Saved topic candidates

Below-threshold concepts do **not** create vault nodes, source links to premature
concepts, or terminal approval prompts. They are retained in SQLite and a readable
Markdown register outside the Obsidian vault:

```text
<data_path>/maintenance/<vault-id>/topic-candidates.md
```

The register lists candidate IDs, aliases, definitions, breadth explanations,
entity kinds, current original-source counts, missing support, and source excerpts.
Excluded and eligible candidates are also visible for auditing. Removed evidence
does not keep contributing to counts; inactive concepts remain recorded for later
rediscovery. A subsequent normal `mynd connect` automatically offers review when
a saved candidate gains enough support. Waiting/excluded candidates alone do not
cause a failure exit. The register path and counts are printed; `--json` includes
all candidate records. `--dry-run` previews these records without writing the
register or database.

To correct a below-threshold candidate's inaccurate breadth classification without
having every candidate interrupt review, use its saved ID:

```bash
mynd connect --reclassify concept-EXAMPLE=umbrella
# Valid categories: umbrella, established, specialized, niche; the option is repeatable.
```

This is a recorded human classification correction, not a count override. The new
category's threshold still applies, and excluded entity types remain excluded.
Keep personal comments outside the register's managed block; edits inside that
checksummed block cause a safe refusal rather than an overwrite.

Older saved approvals are retained but cannot automatically create files under
the new policy. Eligible legacy concepts require renewed review and reuse their
existing topic ID/path when bound to that approval. Below-threshold legacy concepts
remain candidates. Existing legacy files are preserved, not automatically deleted,
merged, or migrated; outstanding legacy approvals are listed in the JSON report.

Approved topics live under `20 Topics/` by default, with stable IDs and filenames.
They contain an **AI-generated synthesis from vault notes**, claim/paragraph-level
note citations, supporting excerpts, a list of mentions, optional web overview,
and a personal **My notes** area. Synthesis targets about 200–500 words when
supported; thin evidence is not padded. Citation validation checks source identity,
not whether an interpretation is correct. Verify important claims yourself.

Each mentioning note receives a managed **Related topics** list and a wikilink at
its first suitable prose mention. Headings, quotations, code, math, URLs, tables,
and existing links are not inline-rewritten. Newly approved topics also search
older, unchanged notes for relevant mentions. Contextual verification distinguishes
different senses of a term rather than blindly linking every keyword match.

Later runs reuse cached extraction, decisions, matches, and summaries. An unchanged,
completed run makes no model calls, web requests, or note writes (unless a web cache
has expired). Failures remain retryable; `--all` forces a fresh local pass. Renames
reuse work IDs, or an unambiguous disappeared-file fingerprint for handwritten
notes. If a rename and content change cannot be identified, the file is treated as
a new note. Deleted/changed evidence updates affected topics; topic files and
personal notes are never automatically deleted. Syntax/ownership problems defer
affected updates rather than overwriting damaged notes.

Discovery and grouping are checkpointed separately. Grouping uses required short-ID
assignments; Python constructs and validates complete partitions. Each successful
batch is saved immediately, so an interruption does not discard earlier work.
Malformed responses receive one repair attempt, then at most two levels of batch
subdivision. Completed children are reused on the next run; only unfinished leaves
are retried. Model/transport outages stop further uncached grouping requests rather
than triggering subdivision. Successful cached batches remain usable.

If grouping still fails, candidates are retained **unmerged**, with a warning in
eligible review proposals and batch/candidate diagnostics in `--json`. Other batches
can still reach review. No fallback approves a new topic automatically, and unresolved
grouping returns **1** even if useful work completes. Rerun `mynd connect` normally
to resume; do **not** use `--all` just to recover from a grouping error. Grouping
schema upgrades do not invalidate note discovery. Newly approved topics do not
invalidate every grouping batch; existing-topic bindings are checked for deletion
or changed meaning. Progress identifies discovery notes, passage batches, and
grouping batches (including cached work).
Breadth/entity assessments and verified candidate evidence have separate caches;
the new policy does not invalidate completed discovery. Failed assessments or
evidence checks leave candidates pending, never implicitly eligible.

### Optional free-tier web overview

Mynd uses [Ollama's hosted search/fetch API](https://docs.ollama.com/capabilities/web-search),
which currently has a [free tier](https://ollama.com/blog/web-search). No additional
server, hosted model, or Ollama Python SDK is needed. Free-tier limits can change;
there is no paid fallback.

```bash
export OLLAMA_API_KEY="your-key"   # Create a free account/key at ollama.com/settings/keys
mynd connect --web
mynd connect --web --refresh-web  # Explicitly refresh cached sources
```

Only approved topic queries and selected public URLs go to Ollama's hosted API,
**not vault text**. Retrieved page content is summarized by your local model. The
short overview is labeled **AI generated**, cites fetched sources, and records its
retrieval date. Search snippets alone are not presented as inspected pages. The
default cache lifetime is 30 days. On network/quota/key failure the supported local
topic can still be written; prior valid web content is retained with a notice and
the command returns **1**. A later run retries the failed stage. `--offline` makes
no web requests and retains an already-generated web section.

Configuration defaults in `config.toml`:

```toml
topics_folder = "20 Topics"
maintenance_excludes = []       # e.g. ["Private/**", "Archive/**"]
topic_web_enabled = false      # Opt in here, or per run with --web
topic_web_cache_days = 30
```

Dotfolders (including `.obsidian`/`.trash`) and symlinks are excluded. Exclusions
apply to both commands. Keep `data_path` outside the actual Obsidian vault so that
backups do not become duplicate graph nodes. Keep API keys in the environment,
never in notes or configuration. For a dry run, select a resource profile beforehand
with `mynd mode silent` instead of combining `--mode` with `--dry-run`.

### Preservation and recovery

Maintenance patches are backed up under
`<data_path>/maintenance/<vault-id>/<run-id>/`. Every writing run prints its ID.
The journal and original file contents are saved before edits; files are replaced
atomically and checked for concurrent edits. Maintenance commands use a per-vault
lock. An interrupted writing run can be recovered using its journal directory's ID:

```bash
mynd clean --restore RUN_ID
```

Recovery refuses to overwrite a file edited after the run. It removes a newly
created topic only if that file still matches the run's exact postimage. Recovery
restores files, **not topic approvals or cached analysis**; a subsequent `connect`
can recreate approved output. Backups contain local note content and should be
protected like the vault itself.

Related-topic sections, generated topic content, and inserted inline links carry
ownership/checksum markers. Do not remove those markers. Manual changes to owned
content cause a safe refusal; place personal edits outside generated blocks.
Paper/course refreshes preserve connector sections and reapply inline links only
when their evidence passage is unchanged. Changed passages lose the generated
inline link and receive a stale-connections notice until `connect` rechecks them.
Paper refreshes also retain custom chapters, frontmatter properties, and reading
status rather than rebuilding the whole note.

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

Vault maintenance stores passage/analysis caches and approvals in the local SQLite
database, with note snapshots/journals under `data_path`. Topic discovery and all
synthesis run locally. Optional `connect --web` sends only approved topic queries
and selected URLs to Ollama's hosted search/fetch service; those queries can reveal
your interests or private topic names. Web access is disabled by default.
