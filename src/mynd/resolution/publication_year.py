"""Conservative publication dates: identifiers and issue numbers are not years."""
from __future__ import annotations

import re
from datetime import date

from mynd.contracts import MAX_YEAR, MIN_YEAR


def valid_year(value: object) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"\d{4}", value.strip()):
        value = int(value)
    if type(value) is int and MIN_YEAR <= value <= min(MAX_YEAR, date.today().year + 1):
        return value
    return None


def publication_year(citation: dict, raw: str) -> int | None:
    explicit = valid_year(citation.get("year"))
    if explicit is not None:
        return explicit
    # A DOI can contain plausible historical dates (arXiv.1604...) as well as
    # impossible future ones (arXiv.2304...). Never infer dates from identifiers.
    text = re.sub(r"(?:https?://|www\.)[^\s<>]+|\b10\.\d{4,9}/[^\s<>]+", " ", raw,
                  flags=re.IGNORECASE)
    text = re.sub(r"(?i)(?:arxiv\s*:\s*)?\b\d{4}\.\d{4,5}(?:v\d+)?\b", " ", text)
    text = re.sub(r"(?i)\b(?:vol(?:ume)?|no|number|issue|pp?|pages?)\.?\s*\d+(?:[-–]\d+)?", " ", text)
    parenthesized = {year for match in re.finditer(r"\(\s*(\d{4})\s*\)", text)
                     if (year := valid_year(match[1])) is not None}
    if parenthesized:
        return next(iter(parenthesized)) if len(parenthesized) == 1 else None
    candidates = {year for match in re.finditer(r"(?<![\d.])\b(\d{4})\b(?!\.\d)", text)
                  if (year := valid_year(match[1])) is not None}
    return next(iter(candidates)) if len(candidates) == 1 else None


def legacy_publication_year(citation: dict, raw: str) -> int | None:
    """Recognize the old writer's exact output, solely for evidence-backed migration."""
    value = citation.get("year")
    if isinstance(value, int) and MIN_YEAR <= value <= MAX_YEAR:
        return value
    for pattern in (r"\(\s*(\d{4})\s*\)", r"\b(\d{4})\b"):
        for source in (str(value or ""), raw):
            match = re.search(pattern, source)
            if match and MIN_YEAR <= int(match[1]) <= MAX_YEAR:
                return int(match[1])
    return None
