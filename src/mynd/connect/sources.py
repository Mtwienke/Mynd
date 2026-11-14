"""Count original works, not copies, derived excerpts, or course overviews."""
from __future__ import annotations

import re

from mynd.resolution.doi import normalize_doi
from mynd.vault.markdown import normalized


def source_identities(notes: dict) -> dict[str, str | None]:
    paths, works = {}, {}
    for identifier, (doc, _) in notes.items():
        paths[normalized(doc.relative)] = identifier
        paths[normalized(doc.relative.removesuffix(".md"))] = identifier
        if doc.metadata.get("work_id"):
            works[doc.metadata["work_id"]] = identifier
    result = {}

    def resolve(identifier, visiting):
        if identifier in result:
            return result[identifier]
        if identifier in visiting:
            return None  # Circular provenance is not independent evidence.
        doc = notes[identifier][0]
        if doc.kind == "course":
            return None
        derived = doc.metadata.get("derived_from")
        original = doc.metadata.get("source_work_id")
        if original is not None:
            if not isinstance(original, str) or not original.strip():
                return None
            target = works.get(original)
            return resolve(target, visiting | {identifier}) if target else "work:" + original
        if derived is not None:
            values = derived if isinstance(derived, list) else [derived]
            roots = set()
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    return None
                value = re.sub(r"^\[\[|\]\]$", "", value).split("|", 1)[0].split("#", 1)[0]
                target = paths.get(normalized(value)) or works.get(value)
                root = resolve(target, visiting | {identifier}) if target else "source:" + normalized(value)
                if root is None:
                    return None
                roots.add(root)
            # Multi-source summaries cannot manufacture independent support;
            # evidence would need passage-level original-source attribution.
            return next(iter(roots)) if len(roots) == 1 else None
        doi = doc.metadata.get("doi")
        if isinstance(doi, str) and (doi := normalize_doi(doi)):
            return "doi:" + doi.casefold()
        return "work:" + doc.metadata["work_id"] if doc.metadata.get("work_id") else identifier

    for identifier in notes:
        result[identifier] = resolve(identifier, set())
    return result


def support_count(source_ids, catalog, identities) -> int:
    return len({identities[catalog[key].note_id] for key in source_ids
                if key in catalog and identities.get(catalog[key].note_id) is not None})
