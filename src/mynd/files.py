from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_REFERENCES_HEADING = re.compile(
    r"(?im)^[ \t]*(?:REFERENCES(?:[ \t]+AND[ \t]+NOTES)?|BIBLIOGRAPHY)[ \t]*$"
)


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, content: str) -> None:
    """Write text via a temporary sibling and atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


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
