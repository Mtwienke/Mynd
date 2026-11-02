"""Extracted PDF text: bibliography trimming and on-disk persistence."""

from __future__ import annotations

import re
from pathlib import Path

from mynd.files import atomic_write_text

_REFERENCES_HEADING = re.compile(
    r"(?im)^[ \t]*(?:REFERENCES(?:[ \t]+AND[ \t]+NOTES)?|BIBLIOGRAPHY)[ \t]*$"
)


def save_extracted_text(data_path: Path, stored_pdf: Path, text: str) -> Path:
    """Persist full PDF text for inspection and troubleshooting."""
    output = data_path / "extracted_text" / f"{stored_pdf.stem}.txt"
    atomic_write_text(output, text)
    return output


def text_without_references(text: str) -> str:
    """Exclude a terminal bibliography from the text sent to the summarizer."""
    for match in _REFERENCES_HEADING.finditer(text):
        if match.start() >= len(text) // 2:
            return text[: match.start()].rstrip()
    return text
