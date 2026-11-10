"""Local PowerPoint export. Only temporary working copies are opened by the app."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

import pymupdf
from defusedxml import ElementTree as ET

_PRESENTATION_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_EXPORT_SCRIPT = '''on run argv
    set inputFile to POSIX file (item 1 of argv) as alias
    set outputFile to POSIX file (item 2 of argv) as alias
    set exportStage to "opening staged presentation"
    set targetPresentation to missing value
    tell application "Microsoft PowerPoint"
        with timeout of 180 seconds
            try
                open inputFile
                repeat with presentationIndex from 1 to count of presentations
                    if name of presentation presentationIndex is (item 3 of argv) then
                        set targetPresentation to presentation presentationIndex
                        exit repeat
                    end if
                end repeat
                if targetPresentation is missing value then error "Staged presentation did not open"
                if not my ownsPresentation(targetPresentation, inputFile, outputFile) then
                    error "Staged presentation identity changed; refusing export"
                end if
                set exportStage to "exporting slide PDF"
                save targetPresentation in outputFile as save as PDF
            on error errorMessage number errorNumber
                my closeOwnPresentation(inputFile, outputFile)
                error (exportStage & ": " & errorMessage) number errorNumber
            end try
            my closeOwnPresentation(inputFile, outputFile)
        end timeout
    end tell
end run

on ownsPresentation(candidate, inputFile, outputFile)
    tell application "Microsoft PowerPoint"
        try
            set candidatePath to full name of candidate
            return candidatePath is (POSIX path of inputFile) or ¬
                candidatePath is (inputFile as text) or ¬
                candidatePath is (POSIX path of outputFile) or ¬
                candidatePath is (outputFile as text)
        on error
            return false
        end try
    end tell
end ownsPresentation

on closeOwnPresentation(inputFile, outputFile)
    tell application "Microsoft PowerPoint"
        try
            repeat with candidate in presentations
                if my ownsPresentation(candidate, inputFile, outputFile) then
                    close candidate saving no
                    exit repeat
                end if
            end repeat
        end try
    end tell
end closeOwnPresentation
'''


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


def convert_powerpoint(source: Path, destination: Path) -> Path:
    """Export a slide-per-page PDF, with bounded execution and no shell interpolation."""
    count = pptx_slide_count(source)
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
        try:
            subprocess.run(
                ["osascript", "-", str(staged), str(output), staged.name],
                input=_EXPORT_SCRIPT, capture_output=True, text=True, check=True, timeout=200,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("PowerPoint PDF export timed out; check PowerPoint for dialogs") from exc
        except subprocess.CalledProcessError as exc:
            raise ValueError(
                "PowerPoint PDF export failed. Open PowerPoint to finish first-run/sign-in "
                "or file-access dialogs; check Automation access in System Settings → Privacy "
                f"& Security. Details: {exc.stderr.strip()[:1000]}"
            ) from exc
        if not output.is_file() or not output.stat().st_size:
            raise ValueError("PowerPoint reported success but did not create a PDF")
        validate_export(output, count)
        shutil.copy2(output, destination)
    return destination
