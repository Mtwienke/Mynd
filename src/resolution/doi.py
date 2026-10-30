import re

DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
_RESOLVER_PATH = "doi.org/"


def normalize_doi(value: str | None) -> str | None:
    """Normalize a DOI URL or text fragment to its DOI identifier."""
    if not value:
        return None
    text = value.strip()
    marker = text.lower().find(_RESOLVER_PATH)
    if marker != -1:
        text = text[marker + len(_RESOLVER_PATH):]
    match = DOI_RE.search(text.rstrip(" .,:;"))
    return match.group(0).rstrip(" .,:;") if match else None
