"""Lossless first-mention overlays and model-free preservation during regeneration."""
from __future__ import annotations

import re
from pathlib import Path

from mynd.vault.managed import block_range, owned_links, put_block, unwrap_links, wrap_link
from mynd.vault.markdown import normalized, parse_document, passages, path_key


def inline_label(value: str) -> str:
    return re.sub(r"[\r\n\[\]|]", " ", value).strip()


def term_pattern(term: str) -> str:
    return ((r"(?<!\w)" if term[0].isalnum() else "") + re.escape(term)
            + (r"(?!\w)" if term[-1].isalnum() else ""))


def connect_note(content: str, note_id: str, assignments: list[dict]) -> str:
    base = unwrap_links(content)
    old_block = block_range(base, "topics")
    if not assignments and not old_block and base == content:
        return content
    doc = parse_document(Path("note.md"), Path("."), base)
    if doc.errors:
        raise ValueError("Source note has syntax/ownership errors; run clean before connecting it")
    catalog = {p.id: p for p in passages(doc, note_id)}
    mask = doc.inline_mask
    if old_block:
        mask = mask[:old_block[0]] + " " * (old_block[1] - old_block[0]) + mask[old_block[1]:]
    patches = []
    taken = []
    existing_targets = {path_key(link.target.removesuffix(".md")) for link in doc.links
                        if not old_block or not old_block[0] <= link.start < old_block[1]}
    for assignment in sorted(assignments, key=lambda item: item["topic_id"]):
        if path_key(assignment["target"]) in existing_targets:
            continue
        possible = []
        for mention in assignment["mentions"]:
            source = catalog.get(mention["source"])
            term = mention["term"]
            if source is None or not term:
                continue
            names = assignment.get("names", [assignment["title"]])
            if not any(name and re.search(term_pattern(name), term, re.IGNORECASE) for name in names):
                continue  # Semantic evidence is not permission to wikilink "it" or "the method".
            for match in re.finditer(term_pattern(term), mask[source.start:source.end], re.IGNORECASE):
                start, end = source.start + match.start(), source.start + match.end()
                if not any(left < end and start < right for left, right in taken):
                    possible.append((start, end))
        if possible:
            start, end = min(possible)
            patches.append((start, end, wrap_link(assignment["topic_id"], assignment["target"],
                                                 base[start:end])))
            taken.append((start, end))
    for start, end, replacement in sorted(patches, reverse=True):
        base = base[:start] + replacement + base[end:]
    heading = "Related topics"
    if ((not old_block and any(h.level == 2 and h.text == heading for h in doc.headings))
            or (old_block and old_block[2].startswith("## Related topics (Mynd)"))):
        heading += " (Mynd)"
    lines = [f"- [[{item['target']}|{inline_label(item['title'])}]]"
             for item in sorted(assignments, key=lambda item: normalized(item["title"]))]
    body = "## " + heading + "\n\n" + ("\n".join(lines) if lines else "_No currently verified topics._")
    return put_block(base, "topics", body)


def preserve_connections(previous: str, rendered: str) -> str:
    """Reapply only exact unchanged evidence passages; never rerun inference here."""
    links = owned_links(previous)
    old_block = block_range(previous, "topics")
    if not links and old_block is None:
        return rendered
    old_base = unwrap_links(previous)
    old_doc = parse_document(Path("note.md"), Path("."), old_base)
    new_doc = parse_document(Path("note.md"), Path("."), rendered)
    old_passages = passages(old_doc, "overlay")
    new_passages = {p.id for p in passages(new_doc, "overlay")}
    assignments = {}
    removed_length = 0
    stale = False
    for link in links:
        position = link.start - removed_length
        removed_length += link.end - link.start - len(link.label)
        source = next((p for p in old_passages if p.start <= position < p.end), None)
        if source is not None and source.id in new_passages:
            assignments[link.topic_id] = {
                "topic_id": link.topic_id, "target": link.target, "title": link.label,
                "mentions": [{"source": source.id, "term": link.label}],
            }
        else:
            stale = True
    if assignments:
        rendered = connect_note(rendered, "overlay", list(assignments.values()))
    if old_block:
        body = old_block[2]
        if stale and "Connections may be stale" not in body:
            body += "\n\n_Connections may be stale after regeneration; run `mynd connect`._"
        rendered = put_block(rendered, "topics", body)
    return rendered
