from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import yaml

from mynd.contracts import MAX_YEAR, MIN_YEAR
from mynd.resolution.publication_year import legacy_publication_year, publication_year, valid_year
from mynd.settings import Settings
from mynd.vault.managed import block_range, owned_links
from mynd.vault.markdown import Document, Link, LinkIndex, parse_document, vault_files
from mynd.vault.safety import Change, apply_changes, digest, maintenance_lock
from mynd.vault.year_notes import (
    YEARS_FOLDER,
    render_year_note,
    validate_year_note,
    year_link,
    year_path,
)

_YEAR_NAME = re.compile(r"(?:year[- :]+)?(\d{4})", re.IGNORECASE)
_PUBLICATIONS = {"paper", "paper-placeholder"}


def _named_year(value: str) -> int | None:
    name = Path(value).name
    match = _YEAR_NAME.fullmatch(name[:-3] if name.lower().endswith(".md") else name)
    return int(match[1]) if match and MIN_YEAR <= int(match[1]) <= MAX_YEAR else None


def _intro_end(doc: Document) -> int:
    return next((h.start for h in doc.headings if h.level == 2), len(doc.content))


def _year_field(doc: Document):
    fields = list(re.finditer(r"(?m)^- \*\*Year:\*\* ([^\r\n]*)", doc.content[:_intro_end(doc)]))
    if len(fields) > 1:
        raise ValueError(f"Ambiguous publication-year fields in {doc.relative}")
    return fields[0] if fields else None


def _correct_old_fallback(doc: Document) -> tuple[str, dict | None]:
    if doc.kind != "paper-placeholder":
        return doc.content, None
    match = re.search(r"(?ms)^## GROBID Dump\r?$.*?^~~~json\r?\n(.*?)^~~~\r?$", doc.content)
    if not match:
        return doc.content, None
    try:
        citation = json.loads(match[1])
    except (ValueError, TypeError):
        return doc.content, None
    if not isinstance(citation, dict) or not isinstance(citation.get("raw_reference"), str):
        return doc.content, None
    raw = citation["raw_reference"]
    old, new = legacy_publication_year(citation, raw), publication_year(citation, raw)
    current = doc.metadata.get("year")
    field = _year_field(doc)
    expected = {str(old), f"[[{old}]]"} if old is not None else {"Unknown"}
    if (new is None or new == current or current != old or field is None
            or field[1].strip() not in expected):
        return doc.content, None
    # Only the exact old generated metadata/field pair is corrected. The original
    # GROBID JSON and citation remain verbatim, and manual date overrides survive.
    value = str(old) if old is not None else "null"
    front = re.search(r"(?m)^year: (" + re.escape(value) + r")(?=[ \t\r]*(?:#|$))", doc.content)
    if front is None:
        return doc.content, None
    content = doc.content[:front.start(1)] + str(new) + doc.content[front.end(1):]
    return content, {"path": doc.relative, "old": old, "year": new,
                     "reason": "Recovered explicit publication date; old fallback matched an identifier/number"}


def _link_publication(doc: Document, year: int, original_year: object) -> str:
    field = _year_field(doc)
    link = year_link(year)
    if field:
        value = field[1]
        leading = re.match(r"(?:\[\[[^\]\r\n]+\]\]|\d{4}|Unknown|Not identified)", value)
        if leading is None:
            raise ValueError(f"Unrecognized custom publication-year field in {doc.relative}")
        old = leading[0]
        if old.startswith("[["):
            target, _, label = old[2:-2].partition("|")
            base, _, anchor = target.partition("#")
            recorded = _named_year(base)
            if recorded is None:
                raise ValueError(f"Custom non-year publication link in {doc.relative}; not overwritten")
            if anchor:
                link = "[[" + year_path(year).with_suffix("").as_posix() + "#" + anchor + "|" + (label or str(year)) + "]]"
            elif label:
                link = "[[" + year_path(year).with_suffix("").as_posix() + "|" + label + "]]"
        else:
            recorded = int(old) if old.isdigit() else None
        if recorded is not None and recorded not in {year, original_year}:
            raise ValueError(f"Publication year and visible year field disagree in {doc.relative}")
        start = field.start(1)
        return doc.content[:start] + link + doc.content[start + len(old):]
    heading = next((h for h in doc.headings if h.level == 1), None)
    if heading:
        position = heading.end
    elif doc.content.startswith(("---\n", "---\r\n")):
        closing = re.search(r"(?m)^---\r?$", doc.content[doc.content.index("\n") + 1:])
        position = doc.content.index("\n") + 1 + closing.end() if closing else 0
    else:
        position = 0
    newline = "\r\n" if "\r\n" in doc.content else "\n"
    return doc.content[:position] + newline + "- **Year:** " + link + newline + doc.content[position:]


def _adopt_year(doc: Document, year: int) -> str:
    missing = []
    for key, value in (("type", "year"), ("work_id", f'"year-{year}"'), ("year", str(year))):
        if key not in doc.metadata:
            missing.append(f"{key}: {value}")
    if not missing:
        return doc.content
    newline = "\r\n" if "\r\n" in doc.content else "\n"
    fields = newline.join(missing) + newline
    if doc.content.startswith(("---\n", "---\r\n")):
        position = doc.content.index("\n") + 1
        return doc.content[:position] + fields + doc.content[position:]
    return "---" + newline + fields + "---" + newline + newline + doc.content


def _rehash_blocks(before: str, after: str) -> str:
    # This operation changes destinations, not generated prose. Re-sign only
    # previously intact owned blocks; an edited block is refused during scanning.
    for key in ("topics", "topic-content"):
        if block_range(before, key) is None:
            continue
        pattern = (r"(?m)(^<!-- mynd:" + re.escape(key) + r":begin -->\n)"
                   r"<!-- mynd:hash:[0-9a-f]{64} -->\n(.*?)\n(^<!-- mynd:"
                   + re.escape(key) + r":end -->)")
        after, count = re.subn(pattern, lambda m: m[1] + "<!-- mynd:hash:" + digest(m[2])
                               + " -->\n" + m[2] + "\n" + m[3], after, flags=re.DOTALL)
        if count != 1:
            raise ValueError(f"Cannot safely update managed block '{key}'")
    return after


def _property_links(doc: Document) -> list[Link]:
    """Update quoted property links without reserializing YAML or changing numbers."""
    if not doc.content.startswith(("---\n", "---\r\n")):
        return []
    start = doc.content.index("\n") + 1
    closing = re.search(r"(?m)^(?:---|\.\.\.)\r?$", doc.content[start:])
    if closing is None:
        return []
    root = yaml.compose(doc.content[start:start + closing.start()], Loader=yaml.SafeLoader)
    result, seen = [], set()

    def visit(node):
        if node is None or id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for _, value in node.value:
                visit(value)
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                visit(value)
        elif isinstance(node, yaml.ScalarNode) and node.tag == "tag:yaml.org,2002:str":
            offset = start + node.start_mark.index
            raw = doc.content[offset:start + node.end_mark.index]
            for match in re.finditer(r"\[\[([^\]\r\n]+)\]\]", raw):
                target, _, label = match[1].partition("|")
                target, _, anchor = target.partition("#")
                result.append(Link(offset + match.start(), offset + match.end(), target, anchor, label))

    visit(root)
    return result


def _rewrite_links(doc: Document, destination: Path, index: LinkIndex,
                   moves: dict[Path, Path], targets: dict[int, Path]) -> str:
    replacements = []
    protected_links = owned_links(doc.content)
    for link in [*doc.links, *_property_links(doc)]:
        if not link.target:
            continue  # Local heading/block references move with their note.
        resolved = index.resolve(doc, link)
        target = None
        if len(resolved) == 1 and resolved[0] in moves:
            target = moves[resolved[0]]
        elif not resolved and ("/" not in link.target or link.target.startswith("Years/")):
            year = _named_year(link.target)
            target = targets.get(year)
        elif destination != doc.path and link.kind == "markdown" and len(resolved) == 1:
            target = moves.get(resolved[0], resolved[0])
        if target is None and destination != doc.path and link.kind == "markdown" and not resolved:
            absolute = (doc.path.parent / link.target).resolve()
            relative = (absolute.relative_to(index.vault.resolve()).as_posix()
                        if absolute.is_relative_to(index.vault.resolve())
                        else os.path.relpath(absolute, destination.parent.resolve()))
        elif target is not None:
            relative = target.relative_to(index.vault).as_posix()
        else:
            continue
        if any(owned.start <= link.start < owned.end for owned in protected_links):
            raise ValueError(f"A topic-owned inline link targets a moved year in {doc.relative}; "
                             "resolve that topic assignment before moving it")
        raw = doc.content[link.start:link.end]
        if link.kind == "wiki":
            tail = raw[2 + len(link.target):-2]
            if "|" not in tail:
                separator = "\\|" if any(start <= link.start < end for start, end in doc.table_ranges) else "|"
                tail += separator + Path(link.target).stem
            replacement = "[[" + relative.removesuffix(".md") + tail + "]]"
        else:
            start, end = link.destination_start - link.start, link.destination_end - link.start
            value = quote(relative, safe="/") + ("#" + quote(link.anchor, safe="/^=#") if link.anchor else "")
            if raw[start:end].startswith("<"):
                value = "<" + value + ">"
            replacement = raw[:start] + value + raw[end:]
        if replacement != raw:
            replacements.append((link.start, link.end, replacement))
    result = doc.content
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return _rehash_blocks(doc.content, result)


def _canvas_moves(path: Path, moves: dict[Path, Path], vault: Path) -> Change | None:
    """Canvas file cards store vault-relative paths outside Markdown."""
    from mynd.vault.safety import read_text
    before = read_text(path)
    json.loads(before)  # Never patch a corrupt/non-JSON canvas speculatively.
    relative_moves = {p.relative_to(vault).as_posix(): dest.relative_to(vault).as_posix()
                      for p, dest in moves.items()}

    def replace(match):
        value = json.loads(match[2])
        return match[1] + json.dumps(relative_moves[value], ensure_ascii=False) if value in relative_moves else match[0]

    after = re.sub(r'("file"\s*:\s*)("(?:\\.|[^"\\])*")', replace, before)
    return Change(path, before, after) if after != before else None


def organize_years(settings: Settings, *, apply: bool = False) -> dict:
    """Plan or apply a complete, backed-up migration; never invokes a model/network."""
    with maintenance_lock(settings, dry_run=not apply):
        files = vault_files(settings)
        documents, issues, corrected, content = {}, [], [], {}
        for path in files:
            if path.suffix.lower() != ".md":
                continue
            try:
                doc = parse_document(path, settings.vault_path)
                if doc.errors:
                    raise ValueError("Syntax/ownership errors; run clean before migrating this note")
                documents[path] = doc
            except (OSError, ValueError) as exc:
                issues.append({"path": path.relative_to(settings.vault_path).as_posix(), "error": str(exc)})
        publication_years = set()
        for path, doc in documents.items():
            content[path] = doc.content
            if doc.kind not in _PUBLICATIONS:
                continue
            revised, correction = _correct_old_fallback(doc)
            revised_doc = parse_document(path, settings.vault_path, revised)
            year = valid_year(revised_doc.metadata.get("year"))
            if year is None:
                if revised_doc.metadata.get("year") is not None:
                    issues.append({"path": doc.relative, "error": "Invalid/implausible publication year; not guessed"})
                continue
            publication_years.add(year)
            content[path] = _link_publication(revised_doc, year, doc.metadata.get("year"))
            if correction:
                corrected.append(correction)
        candidates: dict[int, list[Path]] = {}
        for path, doc in documents.items():
            named = _named_year(path.name)
            if doc.kind == "year" and doc.metadata.get("year") is not None:
                year = valid_year(doc.metadata["year"])
                if year is None:
                    raise ValueError(f"Year note has an invalid publication year: {doc.relative}")
            else:
                year = valid_year(named)
            if year is None or doc.kind in _PUBLICATIONS | {"lecture", "course", "topic"}:
                continue
            if (doc.kind == "year" or path.stem.lower().startswith("year")
                    or path.is_relative_to(settings.vault_path / YEARS_FOLDER) or year in publication_years):
                if named is not None and named != year:
                    raise ValueError(f"Year note name and metadata disagree: {doc.relative}")
                validate_year_note(path, settings.vault_path, year)
                candidates.setdefault(year, []).append(path)
        targets, moves, created = {}, {}, []
        for year in sorted(publication_years | candidates.keys()):
            target = settings.vault_path / year_path(year)
            relative = target.relative_to(settings.vault_path).as_posix()
            if any(fnmatch.fnmatch(relative, pattern) for pattern in settings.maintenance_excludes):
                raise ValueError(f"Year destination is excluded by maintenance_excludes: {relative}")
            sources = candidates.get(year, [])
            if len(sources) > 1:
                raise ValueError(f"Conflicting year notes for {year}: " + ", ".join(str(p) for p in sources))
            if target.exists():
                physical = next((p for p in documents if p == target or p.samefile(target)), None)
                if physical is None:
                    raise ValueError(f"Year destination is unreadable, excluded, or symlinked: {relative}")
                if target.is_symlink() or any(parent.is_symlink() for parent in target.parents
                                               if parent.is_relative_to(settings.vault_path)):
                    raise ValueError(f"Symlinked year destination: {relative}")
                validate_year_note(target, settings.vault_path, year)
                if sources and sources[0] != target and not sources[0].samefile(target):
                    raise ValueError(f"Year destination already exists; refusing to overwrite {relative}")
            if sources:
                source = sources[0]
                if source != target and not (target.exists() and source.samefile(target)):
                    moves[source] = target
                content[source] = _adopt_year(documents[source], year)
            elif not target.exists():
                created.append(target)
            targets[year] = target
        if moves and issues:
            raise ValueError("Cannot safely move year notes while some vault notes are unreadable/invalid; "
                             "fix reported problems first")
        index = LinkIndex(settings.vault_path, files)
        changes, updates = [], []
        for target in created:
            changes.append(Change(target, None, render_year_note(_named_year(target.name))))
        # Destinations precede referring-note edits; old paths are removed last.
        moved_changes, other_changes, deletions = [], [], []
        for path, original in documents.items():
            doc = parse_document(path, settings.vault_path, content[path])
            destination = moves.get(path, path)
            rendered = _rewrite_links(doc, destination, index, moves, targets)
            if parse_document(destination, settings.vault_path, rendered).errors:
                raise ValueError(f"Year migration would damage Markdown in {original.relative}")
            if destination != path:
                moved_changes.append(Change(destination, None, rendered))
                deletions.append(Change(path, original.content, None))
            elif rendered != original.content:
                other_changes.append(Change(path, original.content, rendered))
                updates.append(original.relative)
        changes.extend([*moved_changes, *other_changes, *deletions])
        for path in files:
            if path.suffix.lower() == ".canvas" and moves:
                change = _canvas_moves(path, moves, settings.vault_path)
                if change:
                    changes.insert(len(changes) - len(deletions), change)
                    updates.append(path.relative_to(settings.vault_path).as_posix())
        run_id = apply_changes(settings, changes, "years", dependencies={
            path: doc.content for path, doc in documents.items()
        }) if apply else None
        return {"status": "partial" if issues else "complete", "applied": apply,
                "years": sorted(targets), "created": [p.relative_to(settings.vault_path).as_posix() for p in created],
                "moved": [{"from": p.relative_to(settings.vault_path).as_posix(),
                           "to": dest.relative_to(settings.vault_path).as_posix()} for p, dest in moves.items()],
                "updated": updates, "corrected_years": corrected, "issues": issues,
                "changed": len(changes), "run_id": run_id}
