"""Local PowerPoint export. Only temporary working copies are opened by the app."""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from zipfile import BadZipFile, ZipFile

import pymupdf
from defusedxml import ElementTree as ET

_PRESENTATION_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
logger = logging.getLogger(__name__)

_OWNERSHIP_HANDLER = '''
on ownsPresentation(candidate, inputFile, outputFile)
    try
        tell application "Microsoft PowerPoint"
            set candidatePath to (get full name of candidate) as text
        end tell
        if candidatePath starts with "/" then
            set candidateFile to (POSIX file candidatePath) as alias
        else
            set candidateFile to candidatePath as alias
        end if
        -- Resolve aliases in the script, not inside PowerPoint's application dictionary.
        return candidateFile is inputFile or candidateFile is outputFile
    on error errorMessage number errorNumber
        log ("MYND_OWNERSHIP: " & errorNumber & ": " & errorMessage)
        return false
    end try
end ownsPresentation
'''

_EXPORT_SCRIPT = '''on run argv
    set inputFile to POSIX file (item 1 of argv) as alias
    set outputFile to POSIX file (item 2 of argv) as alias
    set exportStage to "opening staged presentation"
    set targetPresentation to missing value
    tell application "Microsoft PowerPoint"
        with timeout of 180 seconds
            try
                log ("MYND_EXPORT_STAGE: " & exportStage)
                open inputFile
                log "MYND_EXPORT_STAGE: selecting staged presentation"
                set presentationCount to count of presentations
                repeat with presentationIndex from 1 to presentationCount
                    if (get name of presentation presentationIndex) is (item 3 of argv) then
                        set targetPresentation to presentation presentationIndex
                        exit repeat
                    end if
                end repeat
                if targetPresentation is missing value then error "Staged presentation did not open"
                if not my ownsPresentation(targetPresentation, inputFile, outputFile) then
                    error "Staged presentation identity changed; refusing export"
                end if
                set exportStage to "exporting slide PDF"
                log ("MYND_EXPORT_STAGE: " & exportStage)
                -- PowerPoint's save destination must be a file specifier, not an alias.
                save targetPresentation in (POSIX file (item 2 of argv)) as save as PDF
            on error errorMessage number errorNumber
                error (exportStage & ": " & errorMessage) number errorNumber
            end try
            log "MYND_EXPORT_STAGE: export complete"
        end timeout
    end tell
end run
''' + _OWNERSHIP_HANDLER

# A live PowerPoint collection is not a finite AppleScript list. Snapshot its count
# and address actual presentations by index, rather than iterating object specifiers.
_CLEANUP_SCRIPT = '''on run argv
    set inputFile to POSIX file (item 1 of argv) as alias
    set outputFile to POSIX file (item 2 of argv) as alias
    tell application "Microsoft PowerPoint"
        with timeout of 5 seconds
            log "MYND_EXPORT_STAGE: closing staged presentation"
            set presentationCount to count of presentations
            log ("MYND_CLEANUP: checking " & presentationCount & " presentation(s)")
            repeat with presentationIndex from presentationCount to 1 by -1
                set candidate to presentation presentationIndex
                if my ownsPresentation(candidate, inputFile, outputFile) then
                    log "MYND_CLEANUP: closing verified temporary presentation"
                    close candidate saving no
                    return "closed staged presentation"
                end if
            end repeat
            return "staged presentation is no longer open"
        end timeout
    end tell
end run
''' + _OWNERSHIP_HANDLER


def _cleanup_powerpoint(staged: Path, output: Path, debug: Callable[[str], None]) -> None:
    """A bounded, best-effort step; it must never undo or mask the export result."""
    started = monotonic()
    debug("Starting separate PowerPoint cleanup: AppleEvent timeout=5s, process timeout=10s")
    try:
        completed = subprocess.run(
            ["osascript", "-", str(staged), str(output)], input=_CLEANUP_SCRIPT,
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = _export_stderr(getattr(exc, "stderr", None))
        debug(f"PowerPoint cleanup failed after {monotonic() - started:.2f}s: "
              f"{type(exc).__name__}: {exc}; stderr: {stderr.strip() or '(empty)'}")
        logger.warning(
            "PowerPoint temporary-presentation cleanup failed; it does not invalidate an "
            "already validated/saved PDF. Close only the temporary presentation '%s' manually "
            "if it remains open. Other presentations were not targeted. Details: %s",
            staged.name, exc,
        )
    else:
        debug(f"PowerPoint cleanup complete in {monotonic() - started:.2f}s; "
              f"result: {_export_stderr(completed.stdout).strip() or '(empty)'}; "
              f"stderr: {_export_stderr(completed.stderr).strip() or '(empty)'}")


def powerpoint_available() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None and any(
        path.is_dir() for path in (
            Path("/Applications/Microsoft PowerPoint.app"),
            Path.home() / "Applications/Microsoft PowerPoint.app",
        )
    )


def pptx_slide_count(path: Path) -> int:
    try:
        with ZipFile(path) as archive:
            if any(name.lower().endswith("vbaproject.bin") for name in archive.namelist()):
                raise ValueError("Macro-bearing presentations are not supported")
            root = ET.fromstring(archive.read("ppt/presentation.xml"))
            slide_list = root.find(f"{_PRESENTATION_NS}sldIdLst")
            count = len(slide_list) if slide_list is not None else 0
    except (BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"Invalid PowerPoint presentation: {path.name}") from exc
    if not count:
        raise ValueError("PowerPoint presentation contains no slides")
    return count


def validate_export(path: Path, expected_pages: int) -> None:
    with pymupdf.open(path) as pdf:
        if pdf.needs_pass or len(pdf) != expected_pages:
            raise ValueError(
                f"Exported PDF has {len(pdf)} pages for {expected_pages} slides; "
                "refusing potentially incorrect page references (check hidden slides/export layout)"
            )


def _export_stderr(value: str | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def _last_export_stage(stderr: str) -> str:
    stages = [line.split("MYND_EXPORT_STAGE: ", 1)[1].strip().removesuffix("*)").strip()
              for line in stderr.splitlines() if "MYND_EXPORT_STAGE: " in line]
    return stages[-1] if stages else "unknown (PowerPoint emitted no stage marker)"


def convert_powerpoint(source: Path, destination: Path, *,
                       debug: Callable[[str], None] | None = None) -> Path:
    """Export a slide-per-page PDF, with bounded execution and no shell interpolation."""
    count = pptx_slide_count(source)
    debug = debug or (lambda _: None)
    debug(f"PowerPoint export input: {source}; expected slides={count}; destination={destination}")
    if not powerpoint_available():
        raise ValueError(
            "Automatic PPTX conversion requires Microsoft PowerPoint and osascript on macOS. "
            "Install PowerPoint or export the presentation to PDF manually."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="powerpoint-", dir=destination.parent) as directory:
        work = Path(directory)
        # A unique name avoids touching any presentation the user already has open.
        staged = work / f"{work.name}.pptx"
        output = work / "export.pdf"
        shutil.copy2(source, staged)
        # Existing-file aliases carry a sandbox file-access grant to the Office app.
        output.touch()
        debug(f"Staged working copy: {staged}; temporary PDF: {output}")
        debug("Starting osascript: AppleEvent timeout=180s, process timeout=200s")
        started = monotonic()
        try:
            try:
                completed = subprocess.run(
                    ["osascript", "-", str(staged), str(output), staged.name],
                    input=_EXPORT_SCRIPT, capture_output=True, text=True, check=True, timeout=200,
                )
            except subprocess.TimeoutExpired as exc:
                stderr = _export_stderr(exc.stderr)
                debug(f"PowerPoint automation timed out after {monotonic() - started:.2f}s; "
                      f"stderr: {stderr.strip() or '(empty)'}")
                raise ValueError(
                    f"PowerPoint PDF export timed out after {exc.timeout}s; "
                    f"last stage: {_last_export_stage(stderr)}. Check PowerPoint for first-run, "
                    "sign-in, or file-access dialogs, or export this deck to PDF manually."
                ) from exc
            except subprocess.CalledProcessError as exc:
                stderr = _export_stderr(exc.stderr)
                debug(f"PowerPoint automation exit_code={exc.returncode}; "
                      f"elapsed={monotonic() - started:.2f}s; stderr: {stderr.strip() or '(empty)'}")
                raise ValueError(
                    "PowerPoint PDF export failed. Open PowerPoint to finish first-run/sign-in "
                    "or file-access dialogs; check Automation access in System Settings → Privacy "
                    f"& Security. Last stage: {_last_export_stage(stderr)}. "
                    f"Details: {stderr.strip()[:1000]}"
                ) from exc
            debug(f"PowerPoint automation complete in {monotonic() - started:.2f}s; "
                  f"stderr: {_export_stderr(completed.stderr).strip() or '(empty)'}")
            if not output.is_file() or not output.stat().st_size:
                raise ValueError("PowerPoint reported success but did not create a PDF")
            validate_export(output, count)
            debug(f"Validated exported PDF: {count} pages, {output.stat().st_size:,} bytes")
            shutil.copy2(output, destination)
            debug(f"Saved converted PDF: {destination}; cleanup cannot discard this result")
        finally:
            _cleanup_powerpoint(staged, output, debug)
    return destination
