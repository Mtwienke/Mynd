from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

from mynd.ai.topic_policy import MINIMUM_SOURCES, POLICY_VERSION
from mynd.ai.topics import TopicWriter
from mynd.connect.grouping import consolidate_topics
from mynd.connect.models import Decision, Proposal
from mynd.connect.policy import (
    check_review_edits,
    prepare_candidates,
    save_candidates,
    verified_mentions,
)
from mynd.connect.sources import support_count
from mynd.connect.web import OllamaWeb
from mynd.identity import slugify
from mynd.settings import Settings
from mynd.storage.maintenance import MaintenanceStore
from mynd.vault.connections import connect_note, term_pattern
from mynd.vault.managed import validate_markers
from mynd.vault.markdown import PARSER_VERSION, normalized, parse_document, passages, vault_files
from mynd.vault.safety import Change, apply_changes, digest, maintenance_lock, read_text
from mynd.vault.topic_notes import mark_topic_stale, render_topic
from mynd.vault.year_notes import YEARS_FOLDER

Reviewer = Callable[[Proposal, list[dict]], Decision]
_FAILURES = (OSError, ValueError, TypeError, KeyError, RuntimeError, httpx.HTTPError)


def _fingerprint(value) -> str:
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _names(item: dict) -> set[str]:
    return {normalized(name) for name in [item["title"], *item["aliases"]]}


def _scan(settings, store, excluded_paths, errors):
    notes, catalog, seen_ids = {}, {}, {}
    previous = store.items("notes")
    files = vault_files(settings)
    paths = {p.relative_to(settings.vault_path).as_posix() for p in files}
    for path in files:
        if path.suffix.lower() != ".md":
            continue
        relative = path.relative_to(settings.vault_path).as_posix()
        if (relative in excluded_paths or path.is_relative_to(settings.vault_path / settings.topics_folder)
                or path.is_relative_to(settings.vault_path / YEARS_FOLDER)):
            continue
        try:
            if any(char in relative for char in "\r\n[]|#^"):
                raise ValueError("Filename is unsafe for Obsidian links; rename the note before connecting")
            doc = parse_document(path, settings.vault_path)
            if doc.kind in {"paper-placeholder", "topic", "year", "decade"}:
                continue
            if doc.errors:
                raise ValueError("Run clean first: " + "; ".join(x["message"] for x in doc.errors[:3]))
            raw_hash = digest(doc.content)
            identity_hash = _fingerprint([(p.heading, p.text) for p in passages(doc, "identity")])
            work_id = doc.metadata.get("work_id")
            if work_id is not None and not isinstance(work_id, str):
                raise ValueError("work_id must be a string")
            known = [key for key, value in previous.items() if value["path"] == relative]
            # A rename is recognized only when an old path disappeared and exactly
            # one old fingerprint matches. Duplicate content never silently merges.
            renamed = [key for key, value in previous.items()
                       if value["path"] not in paths and (value["raw_hash"] == raw_hash
                           or value.get("identity_hash") == identity_hash)]
            identifier = ("work:" + work_id if work_id else known[0] if len(known) == 1
                          else renamed[0] if len(renamed) == 1 else "note-" + uuid4().hex)
            if identifier in seen_ids:
                raise ValueError(f"Duplicate note identity also found in {seen_ids[identifier]}")
            seen_ids[identifier] = relative
            sources = passages(doc, identifier)
            if len({source.id for source in sources}) != len(sources):
                # Repeated identical passages carry one piece of evidence, not
                # independent support. The first occurrence is sufficient.
                sources = list({p.id: p for p in reversed(sources)}.values())[::-1]
            notes[identifier] = (doc, sources)
            catalog.update({source.id: source for source in sources})
            store.put("notes", identifier, {"path": relative, "raw_hash": raw_hash,
                                             "identity_hash": identity_hash})
        except _FAILURES as exc:
            errors.append({"path": relative, "stage": "scan", "error": str(exc)})
    # Do not let either side of a duplicated work_id participate in inference.
    duplicate_paths = {item["error"].split("also found in ", 1)[1] for item in errors
                       if "also found in " in item["error"]}
    for identifier, (doc, sources) in list(notes.items()):
        if doc.relative in duplicate_paths:
            notes.pop(identifier)
            for source in sources:
                catalog.pop(source.id, None)
    return notes, catalog


def _review_groups(settings, store, groups, catalog, notes, reviewer, reopen, persist, writer, identities):
    topics = store.items("topics")
    seeds, pending = {}, []
    for group in groups:
        if group["status"] in {"excluded", "assessment_pending", "evidence_pending"}:
            continue
        sources, support = set(group["sources"]), set(group["support"])
        proposal_id = "proposal-" + _fingerprint([normalized(group["title"]), sorted(sources)])[:24]
        decision = store.get("decisions", proposal_id, {})
        topic_id = group["topic_id"] or decision.get("topic_id", "")
        if topic_id not in topics:
            topic_id = ""
        legacy_id = ""
        if topic_id and topics[topic_id].get("policy_version") != POLICY_VERSION:
            legacy_id, topic_id = topic_id, ""
        if not topic_id and group["status"] == "eligible":
            possible = [key for key, topic in topics.items() if _names(group) & _names(topic)]
            for doc, _ in notes.values():
                aliases = doc.metadata.get("aliases", [])
                if isinstance(aliases, str):
                    aliases = [aliases]
                if not isinstance(aliases, list):
                    aliases = []
                if normalized(group["title"]) in {
                    normalized(doc.path.stem), *[normalized(a) for a in aliases if isinstance(a, str)]
                }:
                    possible.append(doc.relative)
            proposal = Proposal(
                proposal_id, group["title"], group["definition"], tuple(group["aliases"]),
                tuple(catalog[key] for key in sorted(support)), False, group["reason"],
                tuple(possible), group.get("grouping_warning", ""),
                group["breadth"], group["source_count"], group["minimum_sources"], group["id"],
            )
            previous_action = decision.get("action", "")
            if previous_action != "reject" or reopen:
                merge_targets = [topic for topic in topics.values() if topic.get("policy_version") == POLICY_VERSION]
                choice = reviewer(proposal, merge_targets) if reviewer else Decision("defer")
                if choice.action not in {"approve", "merge", "reject", "defer"}:
                    raise ValueError("Invalid topic review decision")
                breadth = choice.breadth or group["breadth"]
                if breadth not in MINIMUM_SOURCES:
                    raise ValueError("Unknown reviewed breadth category")
                if choice.breadth:
                    store.put("candidate-breadths", group["id"], {"breadth": breadth})
                    group.update(breadth=breadth, minimum_sources=MINIMUM_SOURCES[breadth])
                if choice.action == "merge":
                    if choice.topic_id not in topics:
                        raise ValueError("Review selected an unknown topic ID")
                    if topics[choice.topic_id].get("policy_version") != POLICY_VERSION:
                        raise ValueError("A legacy topic must pass new-policy review before it can be a merge target")
                    topic_id = choice.topic_id
                elif choice.action == "approve":
                    if group["source_count"] < MINIMUM_SOURCES[breadth]:
                        raise ValueError("Cannot approve topic below its original-source threshold")
                    title = choice.title or proposal.title
                    if not title.strip() or len(title) > 120 or any(c in title for c in "\r\n[]|"):
                        raise ValueError("Topic title must be plain, single-line text (1–120 characters)")
                    aliases = list(choice.aliases if choice.aliases is not None else proposal.aliases)
                    if any(not isinstance(a, str) or not a.strip() or len(a) > 120 for a in aliases):
                        raise ValueError("Invalid reviewed aliases")
                    topic_id = legacy_id or "topic-" + uuid4().hex[:16]
                    relative = (topics[legacy_id]["path"] if legacy_id else (Path(settings.topics_folder)
                                / f"{slugify(title)} ({topic_id[-12:]}).md").as_posix())
                    if choice.adopt_path:
                        adopted = settings.vault_path / choice.adopt_path
                        if (adopted.is_symlink() or not adopted.is_file()
                                or not adopted.resolve().is_relative_to(settings.vault_path.resolve())
                                or adopted.suffix.lower() != ".md"):
                            raise ValueError("Adoption requires an existing Markdown file inside the vault")
                        relative = adopted.relative_to(settings.vault_path).as_posix()
                        if any(char in relative for char in "\r\n[]|#^"):
                            raise ValueError("Adopted note path is unsafe for Obsidian links")
                        if relative in {topic["path"] for topic in topics.values()}:
                            raise ValueError("This note is already an approved topic; merge instead")
                    definition = choice.definition or proposal.definition
                    if not definition.strip() or len(definition) > 600:
                        raise ValueError("Concept definition must contain 1–600 characters")
                    kind = check_review_edits(writer, group, title, definition, aliases, breadth,
                                              catalog, identities, store)
                    topic = {"id": topic_id, "title": title.strip(), "definition": definition,
                             "aliases": list(dict.fromkeys([*aliases, proposal.title]))[:24],
                             "query": choice.query or title.strip() + " definition overview",
                              "path": relative, "adopted": bool(choice.adopt_path)}
                    topic.update(breadth=breadth, kind=kind, policy_version=POLICY_VERSION)
                    if len(topic["query"]) > 300 or "\n" in topic["query"]:
                        raise ValueError("Web query must be a single line of at most 300 characters")
                    topics[topic_id] = topic
                    store.put("topics", topic_id, topic)
                store.put("decisions", proposal_id, {"action": choice.action, "topic_id": topic_id})
                if persist:
                    store.save()
                if not topic_id:
                    group["status"] = ("rejected" if choice.action == "reject" else "waiting"
                                       if group["source_count"] < group["minimum_sources"] else "eligible")
                    if group["status"] == "eligible":
                        pending.append(asdict(replace(proposal, breadth=breadth,
                                                      minimum_sources=group["minimum_sources"])))
            else:
                group["status"] = "rejected"
        if topic_id:
            group["status"] = "approved"
            group["topic_id"] = topic_id
            topic = topics[topic_id]
            aliases = list(dict.fromkeys([*topic["aliases"], group["title"], *group["aliases"]]))[:24]
            if aliases != topic["aliases"]:
                topic = dict(topic, aliases=aliases)
                topics[topic_id] = topic
                store.put("topics", topic_id, topic)
            seeds.setdefault(topic_id, set()).update(sources)
    return topics, seeds, pending


def _web_section(settings, store, writer, topic, enabled, refresh, web_client):
    published = store.get("topic-output", topic["id"], {})
    old_web = published.get("web")
    old_paragraphs = published.get("web_paragraphs", [])
    if not enabled:
        return old_web, old_paragraphs, ""
    key = digest(topic["query"])
    web = store.get("web", key)
    expired = True
    if web:
        try:
            expired = (datetime.now(UTC) - datetime.fromisoformat(web["fetched_at"])
                       >= timedelta(days=settings.topic_web_cache_days))
        except (KeyError, TypeError, ValueError):
            pass
    try:
        if web is None or expired or refresh:
            web = web_client.research(topic["query"])
            store.put("web", key, web)
        fingerprint = _fingerprint([writer.version, topic["title"],
                                    [(item["id"], item["text"]) for item in web["sources"]]])
        if published.get("web_fingerprint") == fingerprint:
            paragraphs = old_paragraphs
        else:
            material = []
            for source in web["sources"]:
                # A short overview needs bounded opening passages, not entire websites.
                for start in range(0, min(6000, len(source["text"])), 700):
                    material.append({"id": source["id"], "text": source["text"][start:start + 700]})
            paragraphs = writer.summarize(material, topic["title"], web=True)
        published.update(web_fingerprint=fingerprint)
        store.put("topic-output", topic["id"], published)
        return web, paragraphs, ""
    except _FAILURES as exc:
        return old_web, old_paragraphs, "Web overview pending: " + str(exc)


def connect_vault(settings: Settings, writer: TopicWriter, *, reviewer: Reviewer | None = None,
                  all_notes: bool = False, dry_run: bool = False, reopen: bool = False,
                  web_enabled: bool | None = None, refresh_web: bool = False,
                  web_client: OllamaWeb | None = None, breadth_overrides: dict[str, str] | None = None) -> dict:
    """Reusable coordinator. New topics require explicit review even when unattended."""
    with maintenance_lock(settings, dry_run=dry_run):
        store = MaintenanceStore(settings)
        for identifier, breadth in (breadth_overrides or {}).items():
            if breadth not in MINIMUM_SOURCES or not store.get("topic-candidates", identifier):
                raise ValueError("Reclassification requires a saved candidate ID and a valid breadth category")
            store.put("candidate-breadths", identifier, {"breadth": breadth})
        errors = []
        topics_before = store.items("topics")
        notes, catalog = _scan(settings, store, {t["path"] for t in topics_before.values()}, errors)
        analyzed, cached = 0, 0
        candidates = []
        version = PARSER_VERSION + ":" + writer.version
        progress = getattr(writer, "progress", lambda _: None)
        discovery_notes = [(key, value) for key, value in notes.items() if value[0].kind != "course"]
        for number, (identifier, (doc, sources)) in enumerate(discovery_notes, 1):
            fingerprint = _fingerprint([version, [(p.id, p.text) for p in sources]])
            previous = store.get("analysis", identifier)
            if not all_notes and previous and previous["fingerprint"] == fingerprint:
                items = previous["candidates"]
                cached += 1
                progress(f"Discovery note {number}/{len(discovery_notes)}: cached — {doc.relative}")
            else:
                try:
                    progress(f"Discovery note {number}/{len(discovery_notes)}: {doc.relative}")
                    items = writer.extract(sources)
                    store.put("analysis", identifier, {"fingerprint": fingerprint, "candidates": items})
                    analyzed += 1
                    if not dry_run:
                        store.save()
                except _FAILURES as exc:
                    errors.append({"path": doc.relative, "stage": "discovery", "error": str(exc)})
                    continue
            candidates.extend(item for item in items if all(key in catalog for key in item["sources"]))
        candidates = list({item["id"]: item for item in candidates}.values())
        grouping = consolidate_topics(writer, candidates, list(topics_before.values()), store,
                                      persist=not dry_run)
        errors.extend(grouping.errors)
        policy_groups, identities = prepare_candidates(
            writer, grouping.groups, candidates, catalog, notes, store, errors, persist=not dry_run,
        )
        save_candidates(settings, store, policy_groups, catalog, persist=not dry_run)
        topics, seeds, pending = _review_groups(
            settings, store, policy_groups, catalog, notes, reviewer, reopen, not dry_run,
            writer, identities,
        )
        candidate_register = save_candidates(settings, store, policy_groups, catalog, persist=not dry_run)
        # Adopted notes are no longer discovery/evidence input on this very run.
        topic_paths = {topic["path"] for topic in topics.values()}
        notes = {key: value for key, value in notes.items() if value[0].relative not in topic_paths}
        catalog = {key: p for key, p in catalog.items() if p.path not in topic_paths}
        changes = []
        assignments = {key: [] for key in notes}
        topic_results = []
        legacy_approvals = []
        policy_holds = {entry["topic_id"]: entry["status"] for entry in policy_groups
                        if entry.get("topic_id") and entry["status"] in {
                            "assessment_pending", "evidence_pending", "excluded",
                        }}
        own_web_client = web_client is None
        web_client = web_client or OllamaWeb()
        enabled = settings.topic_web_enabled if web_enabled is None else web_enabled
        try:
            for topic_id, topic in topics.items():
                previous_content = None
                if topic.get("policy_version") != POLICY_VERSION:
                    legacy_approvals.append({"id": topic_id, "title": topic["title"], "path": topic["path"],
                                             "status": "requires_policy_review"})
                    # Legacy approvals do not bypass the new creation policy.
                    # Existing files/content are preserved; no implicit migration.
                    if (settings.vault_path / topic["path"]).exists():
                        old = store.get("matches", topic_id, {}).get("mentions", [])
                        for identifier in notes:
                            valid = [item for item in old if item["source"] in catalog
                                     and catalog[item["source"]].note_id == identifier]
                            if valid:
                                assignments[identifier].append({
                                    "topic_id": topic_id, "target": topic["path"].removesuffix(".md"),
                                    "title": topic["title"], "names": [topic["title"], *topic["aliases"]],
                                    "mentions": valid,
                                })
                    continue
                try:
                    topic_path = settings.vault_path / topic["path"]
                    previous_content = read_text(topic_path) if topic_path.exists() else None
                    if topic_id in policy_holds:
                        raise ValueError("Topic policy/evidence refresh requires attention: " + policy_holds[topic_id])
                    if previous_content:
                        validate_markers(previous_content)
                    output = store.get("topic-output", topic_id, {})
                    unreadable = {item["path"] for item in errors
                                   if item.get("stage") in {"scan", "discovery"} and item.get("path")}
                    if any(path in unreadable for path in output.get("source_notes", {}).values()):
                        raise ValueError("A supporting note could not be read safely; topic update deferred")
                    names = [topic["title"], *topic["aliases"]]
                    prior_sources = {item["source"] for item in store.get("matches", topic_id, {}).get("mentions", [])}
                    relevant = [source for source in catalog.values()
                                if source.id in seeds.get(topic_id, set()) or source.id in prior_sources or any(
                                    name and re.search(term_pattern(name), source.text, re.IGNORECASE)
                                    for name in names)]
                    match_fingerprint = _fingerprint([
                        version, POLICY_VERSION, topic["definition"], names, sorted(p.id for p in relevant),
                    ])
                    previous_matches = store.get("matches", topic_id, {})
                    if not all_notes and previous_matches.get("fingerprint") == match_fingerprint:
                        mentions = previous_matches["mentions"]
                    else:
                        mentions = verified_mentions(writer, topic, relevant, store, force=all_notes)
                        store.put("matches", topic_id, {"fingerprint": match_fingerprint,
                                                        "mentions": mentions})
                    mentions = [item for item in mentions if item["source"] in catalog]
                    supporting = {item["source"] for item in mentions if item["supports"]
                                  and notes[catalog[item["source"]].note_id][0].kind != "course"}
                    if previous_content is None and not supporting:
                        raise ValueError("No verified supporting evidence for this new topic; "
                                          "approval retained. Review evidence or retry with --all")
                    if previous_content is None and support_count(supporting, catalog, identities) < MINIMUM_SOURCES[
                        topic.get("breadth", "established")
                    ]:
                        raise ValueError("Verified original-source support is below this topic's creation threshold")
                    local_fingerprint = _fingerprint([version, topic["title"], sorted(supporting)])
                    if output.get("local_fingerprint") == local_fingerprint:
                        paragraphs = output["paragraphs"]
                    else:
                        material = [{"id": key, "note_id": identities.get(catalog[key].note_id) or catalog[key].note_id,
                                     "text": catalog[key].text} for key in sorted(supporting)]
                        paragraphs = writer.summarize(material, topic["title"])
                        output.update(local_fingerprint=local_fingerprint, paragraphs=paragraphs)
                        store.put("topic-output", topic_id, output)
                    web, web_paragraphs, web_error = _web_section(
                        settings, store, writer, topic, enabled, refresh_web, web_client,
                    )
                    if web_error:
                        errors.append({"topic_id": topic_id, "stage": "web", "error": web_error})
                    # Fall back to note-level citations for ambiguous repeated headings.
                    safe_catalog = {}
                    for key, passage in catalog.items():
                        doc = notes[passage.note_id][0]
                        if passage.heading and sum(normalized(h.text) == normalized(passage.heading)
                                                   for h in doc.headings) > 1:
                            passage = replace(passage, heading="")
                        safe_catalog[key] = passage
                    rendered = render_topic(
                        dict(topic, previous_aliases=output.get("aliases", topic["aliases"])),
                        previous_content, safe_catalog, mentions, paragraphs,
                        web, web_paragraphs, web_error=web_error, model=writer.generator.model,
                    )
                    if parse_document(topic_path, settings.vault_path, rendered).errors:
                        raise ValueError("Generated topic failed Markdown/ownership validation")
                    if rendered != previous_content:
                        changes.append(Change(topic_path, previous_content, rendered))
                    output = store.get("topic-output", topic_id, {})
                    output.update(web=web, web_paragraphs=web_paragraphs,
                                  aliases=topic["aliases"],
                                  source_notes={item["source"]: catalog[item["source"]].path
                                                for item in mentions})
                    store.put("topic-output", topic_id, output)
                    for note_id in {catalog[item["source"]].note_id for item in mentions}:
                        assignments[note_id].append({
                            "topic_id": topic_id, "target": topic["path"].removesuffix(".md"),
                            "title": topic["title"], "names": names, "mentions": [item for item in mentions
                                if catalog[item["source"]].note_id == note_id],
                        })
                    topic_results.append({"id": topic_id, "path": topic["path"],
                                          "mentions": len(mentions), "supporting": len(supporting)})
                except _FAILURES as exc:
                    errors.append({"topic_id": topic_id, "stage": "topic", "error": str(exc)})
                    if previous_content:
                        try:
                            stale = mark_topic_stale(previous_content)
                            if stale != previous_content:
                                changes.append(Change(topic_path, previous_content, stale))
                        except ValueError:
                            pass  # A manually edited/damaged owned block is never replaced.
                    # A failed topic is not permission to remove its source links.
                    for identifier, (doc, _) in notes.items():
                        old = store.get("matches", topic_id, {}).get("mentions", [])
                        valid = [item for item in old if item["source"] in catalog
                                 and catalog[item["source"]].note_id == identifier]
                        if valid and topic_path.exists():
                            assignments[identifier].append({
                                "topic_id": topic_id, "target": topic["path"].removesuffix(".md"),
                                "title": topic["title"], "names": [topic["title"], *topic["aliases"]],
                                "mentions": valid,
                            })
            for identifier, (doc, _) in notes.items():
                try:
                    rendered = connect_note(doc.content, identifier, assignments[identifier])
                    if rendered != doc.content:
                        changes.append(Change(doc.path, doc.content, rendered))
                except _FAILURES as exc:
                    errors.append({"path": doc.relative, "stage": "linking", "error": str(exc)})
            run_id = None if dry_run else apply_changes(settings, changes, "connect", dependencies={
                doc.path: doc.content for doc, _ in notes.values()
            })
            if not dry_run:
                store.save()
        finally:
            if own_web_client:
                web_client.close()
        return {"status": "partial" if errors else "pending_review" if pending else "complete",
                "dry_run": dry_run, "analyzed": analyzed, "cached": cached,
                "changed": len(changes), "run_id": run_id, "topics": topic_results,
                "grouping": dict(grouping.stats, candidates=len(candidates), groups=len(grouping.groups)),
                "candidate_register": candidate_register,
                "legacy_approvals": legacy_approvals,
                "pending": pending, "errors": errors,
                "proposed_paths": [str(change.path.relative_to(settings.vault_path)) for change in changes]}
