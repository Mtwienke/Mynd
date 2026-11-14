"""Conservative topic eligibility and a persistent, outside-vault candidate register."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote

import httpx

from mynd.ai.topic_policy import EXCLUDED_KINDS, KINDS, MINIMUM_SOURCES, POLICY_VERSION
from mynd.ai.topics import InvalidModelOutput, batches
from mynd.connect.sources import source_identities, support_count
from mynd.files import atomic_write_text
from mynd.vault.connections import term_pattern
from mynd.vault.managed import put_block
from mynd.vault.markdown import normalized
from mynd.vault.safety import digest, read_text

_FAILURES = (OSError, ValueError, TypeError, KeyError, RuntimeError, httpx.HTTPError)


def fingerprint(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True))


def concept_id(topic):
    return "concept-" + fingerprint([normalized(topic["title"]), topic["definition"]])[:24]


def validate_assessment(item):
    if (not isinstance(item, dict) or not isinstance(item.get("breadth"), str)
            or item["breadth"] not in MINIMUM_SOURCES or item.get("kind") not in KINDS):
        raise InvalidModelOutput("Invalid breadth/entity assessment")
    if not isinstance(item.get("reason"), str) or not item["reason"].strip():
        raise InvalidModelOutput("Missing breadth explanation")
    parents = item.get("broader")
    if not isinstance(parents, list) or len(parents) > 1:
        raise InvalidModelOutput("Expected at most one broader concept")
    for parent in parents:
        if not isinstance(parent, dict) or any(
            not isinstance(parent.get(key), str) or not parent[key].strip() or len(parent[key]) > limit
            for key, limit in (("title", 120), ("definition", 600))
        ) or not isinstance(parent.get("aliases"), list) or len(parent["aliases"]) > 12 or any(
            not isinstance(alias, str) or not alias.strip() or len(alias) > 120 for alias in parent["aliases"]
        ):
            raise InvalidModelOutput("Invalid broader concept")
    return item


def assess_candidates(writer, entries, store, errors, persist):
    missing = []
    for entry in entries:
        record = {key: entry[key] for key in ("id", "title", "definition", "aliases")}
        key = fingerprint([POLICY_VERSION, writer.version, {k: v for k, v in record.items() if k != "id"}])
        previous = store.get("topic-assessments", key)
        try:
            entry["assessment"] = validate_assessment(previous)
        except InvalidModelOutput:
            missing.append(record | {"cache_key": key})
    by_id = {entry["id"]: entry for entry in entries}
    parts = batches(missing, 3500)
    unavailable = None
    progress = getattr(writer, "progress", lambda _: None)
    for number, part in enumerate(parts, 1):
        progress(f"Policy assessment batch {number}/{len(parts)}: {len(part)} candidates")
        try:
            if unavailable is not None:
                raise RuntimeError(unavailable)
            records = [{k: v for k, v in item.items() if k != "cache_key"} for item in part]
            assessments = writer.classify(records)
            if not isinstance(assessments, dict) or set(assessments) != {item["id"] for item in part}:
                raise InvalidModelOutput("Missing or unknown candidate assessments")
            validated = {key: validate_assessment(value) for key, value in assessments.items()}
            for item in part:
                assessment = validated[item["id"]]
                by_id[item["id"]]["assessment"] = assessment
                store.put("topic-assessments", item["cache_key"], assessment)
            if persist:
                store.save()
        except _FAILURES as exc:
            if not isinstance(exc, InvalidModelOutput):
                unavailable = str(exc)
            errors.append({"stage": "policy", "candidates": [item["id"] for item in part], "error": str(exc)})


def relevant_passages(topic, catalog):
    names = [topic["title"], *topic["aliases"]]
    return [source for source in catalog.values() if source.id in topic.get("sources", []) or any(
        name and re.search(term_pattern(name), source.text, re.IGNORECASE) for name in names
    )]


def verified_mentions(writer, topic, passages, store, *, persist=False, force=False, unavailable=None):
    key = fingerprint([POLICY_VERSION, writer.version, topic["title"], topic["definition"],
                       sorted({normalized(name) for name in [topic["title"], *topic["aliases"]]}),
                       sorted(p.id for p in passages)])
    previous = store.get("candidate-evidence", key)
    allowed = {p.id: p.text for p in passages}

    def validate(mentions):
        if not isinstance(mentions, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("source"), str)
            or item["source"] not in allowed or not isinstance(item.get("term"), str)
            or not item["term"].strip() or item["term"].casefold() not in allowed[item["source"]].casefold()
            or type(item.get("supports")) is not bool for item in mentions
        ):
            raise InvalidModelOutput("Invalid verified candidate evidence")
        return mentions

    if previous is not None and not force:
        try:
            return validate(previous)
        except InvalidModelOutput:
            pass
    if unavailable is not None:
        raise RuntimeError(unavailable)
    mentions = validate(writer.match(topic, passages)) if passages else []
    store.put("candidate-evidence", key, mentions)
    if persist:
        store.save()
    return mentions


def prepare_candidates(writer, groups, candidates, catalog, notes, store, errors, *, persist=False):
    identities = source_identities(notes)
    by_candidate = {item["id"]: item for item in candidates}
    entries = []
    for group in groups:
        members = [by_candidate[key] for key in group["members"] if key in by_candidate]
        sources = {key for member in members for key in member["sources"] if key in catalog}
        support = {key for member in members if member["role"] == "central"
                   for key in member["sources"] if key in catalog}
        identifier = concept_id(group)
        if group.get("grouping_warning"):
            identifier += "-" + fingerprint(sorted(group["members"]))[:8]
        entries.append({**group, "id": identifier, "sources": sorted(sources),
                        "support": sorted(support), "origin_candidates": []})
    assess_candidates(writer, entries, store, errors, persist)
    evidence_unavailable = None

    def evaluate(entry, *, contextual=False):
        nonlocal evidence_unavailable
        assessment = entry.get("assessment")
        entry.update(status="assessment_pending", source_count=support_count(entry["support"], catalog, identities),
                     minimum_sources=None, breadth="unknown", kind="unknown", reason="Assessment pending")
        if assessment is None:
            return
        override = store.get("candidate-breadths", entry["id"], {})
        breadth = override.get("breadth", assessment["breadth"])
        required = MINIMUM_SOURCES[breadth]
        entry.update(breadth=breadth, kind=assessment["kind"], minimum_sources=required, reason=assessment["reason"])
        if entry["kind"] in EXCLUDED_KINDS:
            entry["status"] = "excluded"
            return
        entry["status"] = "waiting"
        if entry["source_count"] >= required or contextual or entry.get("topic_id"):
            try:
                mentions = verified_mentions(writer, entry, relevant_passages(entry, catalog), store,
                                             persist=persist, unavailable=evidence_unavailable)
                entry["support"] = sorted({item["source"] for item in mentions if item["supports"]})
                entry["sources"] = sorted({*entry["sources"], *[item["source"] for item in mentions]})
                entry["source_count"] = support_count(entry["support"], catalog, identities)
                entry["status"] = "eligible" if entry["source_count"] >= required else "waiting"
            except _FAILURES as exc:
                if not isinstance(exc, InvalidModelOutput):
                    evidence_unavailable = str(exc)
                entry["status"] = "evidence_pending"
                errors.append({"stage": "candidate-evidence", "candidate_id": entry["id"], "error": str(exc)})

    for entry in entries:
        evaluate(entry)
    # One nonrecursive broader fallback per suppressed candidate. It is NOT an
    # alias/synonym merge: the narrow candidate remains separately in the register.
    by_title = {normalized(entry["title"]): entry for entry in entries}
    broader_entries, reconsider = [], {}
    for entry in entries:
        if entry["status"] not in {"waiting", "excluded"} or entry.get("assessment", {}).get("kind") == "metadata":
            continue
        for parent in entry.get("assessment", {}).get("broader", []):
            title = normalized(parent["title"])
            if title in {normalized(entry["title"]), *[normalized(a) for a in entry["aliases"]]}:
                continue
            if title not in by_title:
                broader = {**parent, "id": concept_id(parent), "members": [], "topic_id": "",
                           "sources": [], "support": [], "origin_candidates": []}
                by_title[title] = broader
                broader_entries.append(broader)
            broader = by_title[title]
            broader["sources"] = sorted(set(broader["sources"]) | set(entry["sources"]))
            broader["origin_candidates"] = sorted(set(broader["origin_candidates"]) | {entry["id"]})
            reconsider[broader["id"]] = broader
    assess_candidates(writer, broader_entries, store, errors, persist)
    entries.extend(broader_entries)
    for entry in reconsider.values():
        evaluate(entry, contextual=True)
    return entries, identities


def check_review_edits(writer, group, title, definition, aliases, breadth, catalog, identities, store):
    """Editing a proposal is not a back door for excluded entities or unsupported senses."""
    if title == group["title"] and definition == group["definition"]:
        return group["kind"]
    revised = {"title": title, "definition": definition, "aliases": aliases}
    revised.update(id=concept_id(revised), sources=group["sources"])
    errors = []
    assess_candidates(writer, [revised], store, errors, False)
    assessment = revised.get("assessment")
    if assessment is None:
        raise ValueError("Edited proposal could not be assessed: " + errors[0]["error"])
    if assessment["kind"] in EXCLUDED_KINDS:
        raise ValueError("Edited proposal is an excluded entity type")
    mentions = verified_mentions(writer, revised, relevant_passages(revised, catalog), store)
    supporting = {item["source"] for item in mentions if item["supports"]}
    if support_count(supporting, catalog, identities) < MINIMUM_SOURCES[breadth]:
        raise ValueError("Edited proposal is below its original-source threshold")
    return assessment["kind"]


def candidate_register_path(settings) -> Path:
    root = settings.data_path.absolute() / "maintenance" / digest(str(settings.vault_path.resolve()))[:16]
    if root.resolve().is_relative_to(settings.vault_path.resolve()):
        raise ValueError("Candidate register must be outside the Obsidian vault")
    return root / "topic-candidates.md"


def save_candidates(settings, store, entries, catalog, *, persist=False):
    current = {entry["id"] for entry in entries}
    for identifier, old in store.items("topic-candidates").items():
        if identifier not in current:
            store.put("topic-candidates", identifier, dict(old, status="inactive", source_count=0, evidence=[]))
    for entry in entries:
        evidence = [{"source": key, "path": catalog[key].path, "heading": catalog[key].heading,
                     "text": catalog[key].text, "supports": key in entry["support"]}
                    for key in entry["sources"] if key in catalog]
        record = {key: entry.get(key) for key in (
            "id", "title", "definition", "aliases", "breadth", "kind", "reason", "status",
            "source_count", "minimum_sources", "origin_candidates", "grouping_warning",
        )}
        store.put("topic-candidates", entry["id"], record | {"evidence": evidence, "policy_version": POLICY_VERSION})
    path = candidate_register_path(settings)
    records = store.items("topic-candidates")
    if persist:
        store.save()
        if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(settings.data_path.absolute())):
            raise ValueError("Refusing a symlinked candidate register")
        body = [("Generated candidate register. Below-threshold concepts are not vault nodes. "
                 "Counts use current substantive original sources; inactive entries do not contribute."),
                ("Thresholds: umbrella **1**, established **2**, specialized **3**, niche **5**. "
                 "Excluded kinds can never be promoted. New nodes always require approval.")]
        for entry in sorted(records.values(), key=lambda item: (item["status"], normalized(item["title"]))):
            required = entry.get("minimum_sources")
            missing = max(0, required - entry["source_count"]) if required else "unknown"
            body.extend(["## " + entry["title"].replace("\n", " "), f"ID: `{entry['id']}`",
                         f"Status: **{entry['status']}** · Breadth: **{entry['breadth']}** · Kind: **{entry['kind']}**",
                         f"Original sources: **{entry['source_count']}/{required or '?'}** · Missing: **{missing}**",
                         entry["definition"], "Aliases: " + "; ".join(entry["aliases"] or ["none"]),
                         "Classification: " + entry["reason"]])
            if entry.get("grouping_warning"):
                body.append("Warning: " + entry["grouping_warning"])
            if entry.get("origin_candidates"):
                body.append("Contextually verified broader fallback for: " + ", ".join(entry["origin_candidates"]))
            for evidence in entry["evidence"]:
                target = settings.vault_path / evidence["path"]
                uri = "file://" + quote(str(target.resolve()))
                label = "substantive" if evidence["supports"] else "mention / unverified"
                body.append(f"- [{evidence['path']}]({uri}) / {evidence['heading'] or '(body)'} ({label})\n"
                            + "\n".join("  > " + line for line in evidence["text"].splitlines()))
        previous = read_text(path) if path.exists() else None
        rendered = put_block(previous or "# Topic candidates\n", "candidate-register",
                             "\n\n".join(body).replace("<!--", "&lt;!--"))
        if (read_text(path) if path.exists() else None) != previous:
            raise ValueError("Concurrent edit to candidate register")
        if rendered != previous:
            atomic_write_text(path, rendered)
    counts = {status: sum(record["status"] == status for record in records.values())
              for status in sorted({record["status"] for record in records.values()})}
    return {"path": str(path), "counts": counts, "items": list(records.values())}
