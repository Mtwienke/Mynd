"""Normalization restricted to known generated/imported formats."""
from __future__ import annotations

import re

from defusedxml import ElementTree as ET

from mynd.vault.managed import code_ranges

SUMMARY_HEADINGS = (
    "Overview", "Main contributions", "Approach", "Findings", "Limitations and open questions",
)


def summary_markdown(value: str) -> str:
    """Allow only the generated summary's known subsections, nested below Summary."""
    protected = code_ranges(value)

    def heading(match):
        if any(start <= match.start() < end for start, end in protected):
            return match[0]
        title = match[2].strip()
        return f"{match[1]}### {title}" if title in SUMMARY_HEADINGS else match[1] + "\\" + match[0].lstrip()

    value = re.sub(r"(?m)^(\s{0,3})\\?#{1,6}\s+([^\r\n]+)", heading, value)
    protected = code_ranges(value)
    return re.sub(r"<!--", lambda match: match[0] if any(
        start <= match.start() < end for start, end in protected
    ) else "&lt;!--", value)


def jats_abstract(value: str) -> str:
    """Unwrap a known Crossref JATS abstract, not arbitrary user HTML/math."""
    if not value.lstrip().startswith("<jats:") or re.search(
        r"<(?:mml:|math\b|(?:jats:)?(?:tex-math|inline-formula|disp-formula)\b)", value,
    ):
        return value
    try:
        root = ET.fromstring(
            '<abstract xmlns:jats="http://jats.nlm.nih.gov">' + value + '</abstract>'
        )
    except (ET.ParseError, ValueError):
        return value
    return "\n\n".join("".join(child.itertext()).strip() for child in root).strip() or value
