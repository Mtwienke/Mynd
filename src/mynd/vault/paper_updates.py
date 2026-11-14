"""Patch generated paper chapters without reconstructing personal/custom chapters."""
from __future__ import annotations

import re
from pathlib import Path

from mynd.vault.managed import block_range
from mynd.vault.markdown import parse_document

_GENERATED = {"Summary", "Extracted questions", "Abstract", "References", "Provenance"}


def refresh_paper(previous: str, rendered: str) -> str:
    old = parse_document(Path("note.md"), Path("."), previous)
    new = parse_document(Path("note.md"), Path("."), rendered)
    if old.errors:
        raise ValueError("Paper note has syntax/ownership errors; run clean before refreshing it")
    topics = block_range(previous, "topics")
    headings = [h for h in old.headings if h.level == 2
                and (not topics or not topics[0] <= h.start < topics[1])]
    generated = [h for h in new.headings if h.level == 2 and h.text in _GENERATED]
    replacements, missing = [], []
    for heading in generated:
        end = next((h.start for h in new.headings if h.start > heading.start and h.level <= 2),
                   len(rendered))
        body = rendered[heading.end:end]
        matches = [h for h in headings if h.text == heading.text]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous paper chapter '{heading.text}'; refusing regeneration")
        if matches:
            match = matches[0]
            boundaries = [h.start for h in headings if h.start > match.start]
            if topics and topics[0] > match.start:
                boundaries.append(topics[0])
            stop = min(boundaries, default=len(previous))
            replacements.append((match.end, stop, body))
        else:
            missing.append(rendered[heading.start:end])
    for start, end, body in sorted(replacements, reverse=True):
        previous = previous[:start] + body + previous[end:]
    if missing:
        previous += ("" if previous.endswith("\n\n") else "\n\n") + "\n".join(missing)
    # Refresh only the template's metadata lines, never similarly named lines in
    # personal chapters. Preserve custom frontmatter properties and reading status.
    first_section = next((h.start for h in parse_document(Path("note.md"), Path("."), previous).headings
                          if h.level == 2), len(previous))
    prefix, rest = previous[:first_section], previous[first_section:]
    new_first = next(h.start for h in new.headings if h.level == 2)
    for match in re.finditer(r"(?m)^- \*\*[^\n:]+:\*\* [^\n]*$", rendered[:new_first]):
        label = match[0].split("**", 2)[1]
        pattern = r"(?m)^- \*\*" + re.escape(label) + r"\*\* [^\n]*$"
        prefix = re.sub(pattern, lambda _, value=match[0]: value, prefix)
    if prefix.startswith("---\n"):
        close = prefix.find("\n---", 4)
        for key in ("doi", "year"):
            line = re.search(r"(?m)^" + key + r": .*$", rendered[:new_first])
            if line and close >= 0:
                front, suffix = prefix[:close], prefix[close:]
                front = re.sub(r"(?m)^" + key + r": .*$", lambda _, value=line[0]: value, front)
                prefix = front + suffix
    return prefix + rest
