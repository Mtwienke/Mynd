from pathlib import Path

import pymupdf

from resolution.doi import normalize_doi


def extract_text_and_doi(path: Path) -> tuple[str, str | None]:
    """Return extracted text and the first DOI found in PDF metadata or text."""
    with pymupdf.open(path) as pdf:
        metadata = pdf.metadata or {}
        text = "\n\n".join(page.get_text("text") for page in pdf)
    candidates = (metadata.get("subject", ""), metadata.get("keywords", ""), text[:30000])
    doi = next((found for item in candidates if (found := normalize_doi(item))), None)
    return text, doi
