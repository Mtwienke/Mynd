import re


DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)


def normalize_doi(value: str | None) -> str | None:
    """Normalize a DOI URL or text fragment to its DOI identifier."""
    if not value:
        return None
    value = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", value.strip(), flags=re.IGNORECASE)
    match = DOI_RE.search(value.rstrip(" .,:;"))
    return match.group(0).rstrip(" .,:;") if match else None
