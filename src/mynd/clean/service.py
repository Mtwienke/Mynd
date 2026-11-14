from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

from mynd.settings import Settings
from mynd.storage.maintenance import MaintenanceStore
from mynd.vault.formatting import SUMMARY_HEADINGS, jats_abstract
from mynd.vault.managed import block_range, owned_links
from mynd.vault.markdown import (
    PARSER_VERSION,
    Document,
    LinkIndex,
    mask_ranges,
    normalized,
    parse_document,
    path_key,
    vault_files,
)
from mynd.vault.safety import Change, apply_changes, digest, maintenance_lock

CLEAN_VERSION = "clean-v2:" + PARSER_VERSION


@dataclass(frozen=True)
class Repair:
    start: int
    end: int
    replacement: str
    rule: str


def _inspect(doc: Document, index: LinkIndex, documents: dict[Path, Document]):
    findings = [dict(error, status="needs_review") for error in doc.errors]
    repairs = []

    def finding(rule, message, position=0, status="needs_review"):
        findings.append({"rule": rule, "message": message, "status": status,
                         "line": doc.content.count("\n", 0, position) + 1})

    if any(error["rule"] == "managed-block-damaged" for error in doc.errors):
        return findings, repairs
    protected = list(doc.protected)
    protected += [(link.start, link.end) for link in owned_links(doc.content)]
    for key in ("topics", "topic-content"):
        block = block_range(doc.content, key)
        if block:
            protected.append((block[0], block[1]))
    if doc.kind == "paper":
        for position, heading in enumerate(doc.headings):
            if heading.level != 2 or heading.text not in {"Summary", "Abstract"}:
                continue
            end = next((h.start for h in doc.headings[position + 1:] if h.level <= 2),
                       len(doc.content))
            if heading.text == "Summary":
                mask = mask_ranges(doc.content, protected)
                for match in re.finditer(r"(?m)^ {0,3}\\#{2,3}\s+([^\r\n]+)",
                                         mask[heading.end:end]):
                    if match[1].strip() in SUMMARY_HEADINGS:
                        start = heading.end + match.start()
                        repairs.append(Repair(start, heading.end + match.end(),
                                              "### " + match[1].strip(), "escaped-summary-heading"))
            else:
                original = doc.content[heading.end:end]
                stripped = original.strip()
                normalized_abstract = jats_abstract(stripped)
                if normalized_abstract != stripped:
                    start = heading.end + original.index(stripped)
                    repairs.append(Repair(start, start + len(stripped), normalized_abstract,
                                          "jats-abstract"))
    for link in doc.links:
        targets = index.resolve(doc, link)
        replacement = None
        if not targets:
            suffix = re.search(r"\(([0-9a-f]{12})\)(?:\.md)?$", link.target)
            if suffix:
                targets = [path for path, other in documents.items()
                           if str(other.metadata.get("work_id", "")).endswith(suffix[1])]
                if len(targets) == 1:
                    replacement = targets[0]
            if not targets:
                is_year = re.fullmatch(r"(?:Years/(?:\d{2}|\d{4})s/year-)?\d{4}(?:\.md)?", link.target)
                rule = "unresolved-year-link" if is_year else "unresolved-link"
                finding(rule, f"No target found: {link.target}", link.start,
                        "unresolved")
                continue
        if len(targets) > 1:
            finding("ambiguous-link", f"Multiple targets for {link.target}", link.start)
            continue
        target = targets[0]
        relative = target.relative_to(index.vault).as_posix()
        display_path = relative.removesuffix(".md")
        expected = link.target.removesuffix(".md")
        actual = display_path if "/" in expected else Path(display_path).name
        if path_key(expected) == path_key(actual) and expected != actual:
            replacement = target
            finding("link-case", f"Capitalization differs from {relative}", link.start,
                    "portability")
        if replacement is not None and link.kind == "wiki":
            value = doc.content[link.start:link.end]
            tail = value[2 + len(link.target):-2]
            value = "[[" + display_path + tail + "]]"
        elif replacement is not None:
            value = doc.content[link.start:link.end]
            start, end = link.destination_start - link.start, link.destination_end - link.start
            old = value[start:end]
            new = quote(relative, safe="/") + ("#" + quote(link.anchor) if link.anchor else "")
            if old.startswith("<"):
                new = "<" + new + ">"
            value = value[:start] + new + value[end:]
        if replacement is not None:
            if any(start <= link.start < end for start, end in protected):
                finding("managed-link", "Run connect to update this checksummed link", link.start)
            else:
                repairs.append(Repair(link.start, link.end, value, "canonical-link-target"))
        if link.anchor and target.suffix.lower() == ".md":
            other = documents.get(target)
            if other is None:
                finding("unreadable-link-target", f"Cannot validate the anchor in unreadable target {relative}",
                        link.start)
                continue
            if link.anchor.startswith("^"):
                present = re.search(r"(?m)(?:^|\s)" + re.escape(link.anchor) + r"\s*$",
                                    mask_ranges(other.content, other.protected))
            else:
                # Obsidian supports nested headings separated by additional hashes.
                names = {normalized(h.text) for h in other.headings}
                present = all(normalized(part) in names for part in link.anchor.split("#"))
            if not present:
                finding("unresolved-anchor", f"Anchor not found: {link.anchor}", link.start,
                        "unresolved")
    mask = mask_ranges(doc.content, protected)
    for match in re.finditer(r"(?m)^#{1,6}[A-Za-z][^\n]*", mask):
        finding("possible-heading-spacing", "Heading or hashtag? Add a space if a heading was intended",
                match.start())
    for delimiter in ("**", "__"):
        markers = list(re.finditer(r"(?<!\\)" + re.escape(delimiter), mask))
        if len(markers) % 2:
            finding("possible-unclosed-emphasis", f"Unmatched {delimiter} delimiter; check the intended formatting",
                    markers[-1].start())
    # Diagnostic only: guessed table repairs can move content between columns.
    lines = mask.splitlines(keepends=True)
    offset = 0
    columns = None
    for line in lines:
        cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
        separator = len(cells) > 1 and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)
        if separator:
            columns = len(cells)
        elif columns is not None:
            if "|" not in line or not line.strip():
                columns = None
            elif len(cells) != columns:
                finding("possible-table-columns", "Table row has a different number of columns", offset)
        offset += len(line)
    for repair in repairs:
        finding(repair.rule, "Mechanical repair available", repair.start, "fixable")
    return findings, repairs


def _repair(content: str, repairs: list[Repair]) -> str:
    boundary = len(content)
    for repair in sorted(repairs, key=lambda item: item.start, reverse=True):
        if repair.end > boundary:
            raise ValueError("Overlapping cleanup repairs; review the note manually")
        content = content[:repair.start] + repair.replacement + content[repair.end:]
        boundary = repair.start
    return content


def clean_vault(settings: Settings, *, apply: bool = False, all_notes: bool = False) -> dict:
    with maintenance_lock(settings):
        store = MaintenanceStore(settings)
        files = vault_files(settings)
        documents = {}
        results = []
        for path in files:
            if path.suffix.lower() != ".md":
                continue
            try:
                documents[path] = parse_document(path, settings.vault_path)
            except (OSError, UnicodeError, ValueError) as exc:
                results.append({"path": str(path), "status": "skipped", "error": str(exc)})
        index = LinkIndex(settings.vault_path, files)
        topology = digest(index.fingerprint() + repr([
            (doc.relative, [h.text for h in doc.headings],
             re.findall(r"(?m)(?:^|\s)\^([A-Za-z0-9-]+)\s*$", doc.content),
             doc.metadata.get("work_id")) for doc in documents.values()
        ]))
        changes = []
        checked = 0
        cached = 0
        for path, doc in documents.items():
            fingerprint = digest(CLEAN_VERSION + topology + doc.content)
            previous = store.get("clean", doc.relative)
            if not all_notes and previous and previous["fingerprint"] == fingerprint:
                findings = previous["findings"]
                repairs = [Repair(**item) for item in previous["repairs"]]
                cached += 1
            else:
                findings, repairs = _inspect(doc, index, documents)
                checked += 1
            result = {"path": doc.relative, "findings": findings, "repairs": len(repairs)}
            if apply and repairs:
                after = _repair(doc.content, repairs)
                changes.append(Change(path, doc.content, after))
                result["status"] = "fixed"
                result["findings"] = [dict(item, status="fixed")
                                      if item["status"] == "fixable" else item for item in findings]
            else:
                result["status"] = "needs_review" if findings else "ok"
            results.append(result)
            store.put("clean", doc.relative, {"fingerprint": fingerprint,
                                              "findings": findings,
                                              "repairs": [asdict(item) for item in repairs]})
        run_id = apply_changes(settings, changes, "clean", dependencies={
            doc.path: doc.content for doc in documents.values()
        }) if apply else None
        # Old hashes intentionally invalidate repaired files on the next inspection.
        store.save()
        return {"status": "complete", "checked": checked, "cached": cached,
                "changed": len(changes), "run_id": run_id, "notes": results}
