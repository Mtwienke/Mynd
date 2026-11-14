"""Resumable, bounded consolidation; malformed output never discards good batches."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from mynd.ai.topics import CONSOLIDATION_VERSION, InvalidModelOutput, consolidation_batches
from mynd.connect.progress import names, progress_for
from mynd.storage.maintenance import MaintenanceStore
from mynd.vault.markdown import normalized
from mynd.vault.safety import digest

_MAX_SPLIT_DEPTH = 2
_FAILURES = (OSError, ValueError, TypeError, KeyError, RuntimeError, httpx.HTTPError)


@dataclass
class GroupingResult:
    groups: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {
        "batches": 0, "completed_batches": 0, "cached_batches": 0,
        "attempted_batches": 0, "split_batches": 0, "unresolved_candidates": 0,
    })


def _fingerprint(value) -> str:
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _binding(topic: dict) -> str:
    # Added aliases/approvals must not make every completed batch cold. A binding
    # is invalid only when its topic disappears or its title/meaning changes.
    return _fingerprint([topic["id"], topic["title"], topic["definition"]])


def _partition(groups, candidates, known) -> list[dict]:
    """Validate cached results as strictly as model results, including a full partition."""
    allowed = {item["id"] for item in candidates}
    if not isinstance(groups, list) or not groups:
        raise InvalidModelOutput("Expected nonempty topic groups")
    result, seen = [], set()
    for group in groups:
        if not isinstance(group, dict):
            raise InvalidModelOutput("Expected a topic group object")
        for key, limit in (("title", 120), ("definition", 600)):
            value = group.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise InvalidModelOutput(f"Invalid group {key}")
        aliases, members, topic_id = (group.get(key) for key in ("aliases", "members", "topic_id"))
        if not isinstance(aliases, list) or len(aliases) > 12 or any(
            not isinstance(alias, str) or not alias.strip() or len(alias) > 120 for alias in aliases
        ):
            raise InvalidModelOutput("Invalid group aliases")
        if not isinstance(topic_id, str) or topic_id and topic_id not in known:
            raise InvalidModelOutput("Unknown existing topic binding")
        if not isinstance(members, list) or not members or any(
            not isinstance(member, str) or member not in allowed for member in members
        ):
            raise InvalidModelOutput("Invalid or unknown group members")
        if len(set(members)) != len(members) or seen.intersection(members):
            raise InvalidModelOutput("Duplicate candidate membership")
        seen.update(members)
        result.append({"title": group["title"], "definition": group["definition"],
                       "aliases": list(aliases), "members": list(members), "topic_id": topic_id})
    if seen != allowed:
        raise InvalidModelOutput("Missing candidate membership: " + ", ".join(sorted(allowed - seen)))
    return result


def _merge(groups: list[dict]) -> list[dict]:
    grouped = {}
    for group in groups:
        # A failed candidate stays explicitly unmerged, even if another batch
        # produced the same title. A human can still approve or merge it in review.
        unresolved = tuple(group["members"]) if group.get("grouping_warning") else ()
        key = (normalized(group["title"]), group["topic_id"], unresolved)
        if key not in grouped:
            grouped[key] = dict(group, members=list(group["members"]), aliases=list(group["aliases"]))
        else:
            grouped[key]["members"].extend(group["members"])
            grouped[key]["aliases"] = list(dict.fromkeys([
                *grouped[key]["aliases"], *group["aliases"],
            ]))[:12]
    return list(grouped.values())


def consolidate_topics(writer, candidates: list[dict], topics: list[dict], store: MaintenanceStore,
                       *, persist: bool = False) -> GroupingResult:
    """Checkpoint each valid batch, isolate bad output, and retry only unfinished leaves."""
    result = GroupingResult()
    known = {topic["id"]: topic for topic in topics}
    by_id = {item["id"]: item for item in candidates}
    parts = consolidation_batches(candidates)
    result.stats["batches"] = len(parts)
    progress = progress_for(writer)
    backend_error = None

    def checkpoint(key, value):
        store.put("group-batches", key, value)
        if persist:
            store.save()

    def fallback(batch, label, error):
        warning = "Grouping unfinished; retained unmerged for review/retry. " + error
        ids = [item["id"] for item in batch]
        result.stats["unresolved_candidates"] += len(ids)
        result.errors.append({"stage": "consolidation", "batch": label,
                              "candidates": ids, "error": warning})
        progress(f"{label}: retaining {len(batch)} unmerged candidate(s) for review/retry")
        return [{**{key: item[key] for key in ("title", "definition", "aliases")},
                 "members": [item["id"]], "topic_id": "", "grouping_warning": warning}
                for item in batch], False

    def process(batch, label, depth=0):
        nonlocal backend_error
        context = label + ": " + names(item["title"] for item in batch)
        progress.focus(context)
        key = _fingerprint([CONSOLIDATION_VERSION, writer.version, batch])
        previous = store.get("group-batches", key, {})
        if not isinstance(previous, dict):
            previous = {}
        if previous.get("status") == "complete":
            try:
                groups = _partition(previous.get("groups"), batch, known)
                bindings = {group["topic_id"] for group in groups if group["topic_id"]}
                if previous.get("bindings") == {identifier: _binding(known[identifier])
                                                for identifier in bindings}:
                    result.stats["cached_batches"] += 1
                    progress(f"{label}: cached ({len(batch)} candidates)")
                    return groups, True
            except InvalidModelOutput:
                pass  # Stale/corrupt cached output is retried, never used as evidence.
        split = previous.get("status") == "split" and depth < _MAX_SPLIT_DEPTH and len(batch) > 1
        if not split:
            if backend_error is not None:
                return fallback(batch, label, backend_error)
            progress(f"{label}: {len(batch)} candidates")
            result.stats["attempted_batches"] += 1
            try:
                full = [by_id[item["id"]] for item in batch]
                groups = _partition(writer.consolidate(full, topics), batch, known)
            except InvalidModelOutput as exc:
                if len(batch) <= 1 or depth >= _MAX_SPLIT_DEPTH:
                    return fallback(batch, label, str(exc))
                progress(f"{label}: invalid output; splitting batch ({exc})")
                # Remember the split before recursing, so a resumed run skips the
                # failed parent and reuses any already completed child partitions.
                checkpoint(key, {"status": "split"})
                split = True
            except _FAILURES as exc:
                # Availability/transport/runtime failures are not malformed model
                # output. Stop new requests for this run rather than fan out retries.
                backend_error = str(exc)
                return fallback(batch, label, backend_error)
        if split:
            result.stats["split_batches"] += 1
            middle = len(batch) // 2
            left, left_ok = process(batch[:middle], label + ".1", depth + 1)
            right, right_ok = process(batch[middle:], label + ".2", depth + 1)
            groups = left + right
            progress.focus(context)
            if not left_ok or not right_ok:
                return groups, False
        bindings = {group["topic_id"] for group in groups if group["topic_id"]}
        checkpoint(key, {"status": "complete", "groups": groups,
                         "bindings": {identifier: _binding(known[identifier]) for identifier in bindings}})
        return groups, True

    for index, batch in progress.track(3, "Grouping", parts, "batches",
                                       lambda batch: names(item["title"] for item in batch)):
        groups, complete = process(batch, f"Grouping batch {index}/{len(parts)}")
        result.groups.extend(groups)
        result.stats["completed_batches"] += int(complete)
    result.groups = _merge(result.groups)
    return result
