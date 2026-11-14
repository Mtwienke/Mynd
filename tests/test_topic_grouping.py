from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from mynd.ai.topics import InvalidModelOutput, consolidation_batches
from mynd.connect.grouping import consolidate_topics
from mynd.settings import load_settings
from mynd.storage.maintenance import MaintenanceStore


@pytest.fixture
def settings(config_file):
    return load_settings(config_file)


def candidates(count, *, large=False):
    return [{"id": f"candidate-{index}", "title": f"Concept {index}", "aliases": [],
             "definition": "A precise concept definition. " + ("x" * 550 if large else "")}
            for index in range(count)]


class Writer:
    version = "test-extraction-v1"

    def __init__(self, fail=None):
        self.calls, self.messages = [], []
        self.fail = fail or (lambda _: None)
        self.progress = self.messages.append

    def consolidate(self, items, known):
        self.calls.append([item["id"] for item in items])
        failure = self.fail(items)
        if failure:
            raise failure
        return [{"title": item["title"], "definition": item["definition"], "aliases": [],
                 "members": [item["id"]], "topic_id": ""} for item in items]


def assert_partition(result, items):
    members = [member for group in result.groups for member in group["members"]]
    assert sorted(members) == sorted(item["id"] for item in items)


def test_completed_batches_checkpoint_before_interruption_and_resume_without_discovery(settings):
    items = candidates(14, large=True)
    parts = consolidation_batches(items)
    assert len(parts) >= 3
    store = MaintenanceStore(settings)

    class Interrupted(Writer):
        def consolidate(self, items, known):
            if self.calls:
                raise KeyboardInterrupt
            return super().consolidate(items, known)

    writer = Interrupted()
    with pytest.raises(KeyboardInterrupt):
        consolidate_topics(writer, items, [], store, persist=True)
    persisted = MaintenanceStore(settings)
    assert len(persisted.items("group-batches")) == 1
    writer = Writer()
    result = consolidate_topics(writer, items, [], persisted, persist=True)
    assert result.stats["cached_batches"] == 1
    assert result.stats["completed_batches"] == len(parts)
    assert len(writer.calls) == len(parts) - 1
    assert_partition(result, items)
    writer = Writer(lambda _: AssertionError("No completed batch should be repeated"))
    repeated = consolidate_topics(writer, items, [], MaintenanceStore(settings))
    assert repeated.stats["cached_batches"] == len(parts) and not writer.calls


def test_invalid_batch_splits_and_keeps_successful_children(settings):
    items = candidates(8)
    writer = Writer(lambda batch: InvalidModelOutput("missing C3") if len(batch) > 4 else None)
    result = consolidate_topics(writer, items, [], MaintenanceStore(settings), persist=True)
    assert [len(call) for call in writer.calls] == [8, 4, 4]
    assert result.stats["split_batches"] == result.stats["completed_batches"] == 1
    assert not result.errors and not result.stats["unresolved_candidates"]
    assert_partition(result, items)
    repeated = consolidate_topics(Writer(), items, [], MaintenanceStore(settings))
    assert repeated.stats["cached_batches"] == 1 and repeated.stats["attempted_batches"] == 0


def test_failed_leaves_remain_unmerged_and_only_failed_leaves_retry(settings):
    items = candidates(8)
    # The same title must not cause fallback candidates to silently merge.
    items = [dict(item, title="Same surface wording") for item in items]
    writer = Writer(lambda batch: InvalidModelOutput("missing C0")
                    if any(item["id"] == "candidate-7" for item in batch) else None)
    result = consolidate_topics(writer, items, [], MaintenanceStore(settings), persist=True)
    assert [len(call) for call in writer.calls] == [8, 4, 4, 2, 2]
    assert result.stats["unresolved_candidates"] == 2
    assert len([g for g in result.groups if g.get("grouping_warning")]) == 2
    assert result.errors[0]["candidates"] == ["candidate-6", "candidate-7"]
    assert_partition(result, items)
    stored = MaintenanceStore(settings).items("group-batches")
    assert sorted(value["status"] for value in stored.values()) == ["complete", "complete", "split", "split"]
    writer = Writer()
    retried = consolidate_topics(writer, items, [], MaintenanceStore(settings), persist=True)
    assert writer.calls == [["candidate-6", "candidate-7"]]
    assert retried.stats["cached_batches"] == 2 and not retried.errors
    assert_partition(retried, items)


def test_always_invalid_output_has_a_fixed_split_bound_and_never_caches_fallback(settings):
    items = candidates(12)
    writer = Writer(lambda _: InvalidModelOutput("Malformed assignment"))
    store = MaintenanceStore(settings)
    result = consolidate_topics(writer, items, [], store, persist=True)
    assert len(writer.calls) == 7  # Parent + two children + four leaves, never unbounded recursion.
    assert result.stats["unresolved_candidates"] == len(items)
    assert len(result.groups) == len(items) and len(result.errors) == 4
    assert all(value["status"] == "split" for value in store.items("group-batches").values())
    assert_partition(result, items)


@pytest.mark.parametrize("error", [RuntimeError("Interrupted backend"), ValueError("Model unavailable"),
                                 httpx.ConnectError("Ollama is down")])
def test_backend_failures_do_not_split_or_repeat_uncached_requests(settings, error):
    items = candidates(15, large=True)
    writer = Writer(lambda _: error)
    result = consolidate_topics(writer, items, [], MaintenanceStore(settings), persist=True)
    assert len(writer.calls) == 1 and result.stats["split_batches"] == 0
    assert result.stats["unresolved_candidates"] == len(items)
    assert not MaintenanceStore(settings).items("group-batches")
    assert_partition(result, items)


def test_later_cached_batches_remain_usable_during_an_outage(settings):
    items = candidates(15, large=True)
    parts = consolidation_batches(items)
    last_ids = {item["id"] for item in parts[-1]}
    consolidate_topics(Writer(), [item for item in items if item["id"] in last_ids], [],
                       MaintenanceStore(settings), persist=True)
    writer = Writer(lambda _: RuntimeError("Ollama stopped"))
    result = consolidate_topics(writer, items, [], MaintenanceStore(settings))
    assert len(writer.calls) == 1 and result.stats["cached_batches"] == 1
    assert result.stats["unresolved_candidates"] == len(items) - len(last_ids)
    assert_partition(result, items)


def test_new_approvals_do_not_invalidate_unbound_partitions_but_bound_topics_are_checked(settings):
    items = candidates(3)
    known = {"id": "topic-existing", "title": "Concept 0", "definition": "A precise definition.", "aliases": []}
    store = MaintenanceStore(settings)
    consolidate_topics(Writer(), items, [], store)
    writer = Writer()
    result = consolidate_topics(writer, items, [known], store)
    assert not writer.calls and result.stats["cached_batches"] == 1
    # Build a separate cache in which the model selected an existing topic.
    store = MaintenanceStore(settings)
    def bind(batch, topics):
        return [{"title": known["title"], "definition": known["definition"], "aliases": [],
                 "members": [item["id"] for item in batch], "topic_id": "topic-existing"}]
    bound_writer = SimpleNamespace(version=writer.version, consolidate=bind)
    consolidate_topics(bound_writer, items, [known], store)
    cached = consolidate_topics(writer, items, [dict(known, aliases=["New synonym"])], store)
    assert not writer.calls and cached.stats["cached_batches"] == 1
    changed = consolidate_topics(writer, items, [dict(known, definition="A different sense.")], store)
    assert writer.calls and not changed.stats["cached_batches"]
    # Deleted bindings cannot route candidates into a nonexistent approved topic.
    store = MaintenanceStore(settings)
    consolidate_topics(bound_writer, items, [known], store)
    deleted = consolidate_topics(Writer(), items, [], store)
    assert deleted.stats["cached_batches"] == 0
    assert all(not group["topic_id"] for group in deleted.groups)


@pytest.mark.parametrize("bad_groups", [
    [], [{"title": "Only one", "definition": "Definition", "aliases": [], "members": ["candidate-0"], "topic_id": ""}],
    [{"title": "Duplicate", "definition": "Definition", "aliases": [],
      "members": ["candidate-0", "candidate-0", "candidate-1"], "topic_id": ""}],
])
def test_corrupt_cached_partitions_are_recomputed(settings, bad_groups):
    items, store = candidates(2), MaintenanceStore(settings)
    consolidate_topics(Writer(), items, [], store)
    key, value = next(iter(store.items("group-batches").items()))
    store.put("group-batches", key, dict(value, groups=bad_groups))
    writer = Writer()
    result = consolidate_topics(writer, items, [], store)
    assert len(writer.calls) == 1 and result.stats["cached_batches"] == 0
    assert_partition(result, items)


@pytest.mark.parametrize("payload", [None, [], "not a cache record"])
def test_nonobject_batch_cache_is_ignored(settings, payload):
    items, store = candidates(2), MaintenanceStore(settings)
    consolidate_topics(Writer(), items, [], store)
    key = next(iter(store.items("group-batches")))
    store.put("group-batches", key, payload)
    writer = Writer()
    result = consolidate_topics(writer, items, [], store)
    assert len(writer.calls) == 1 and result.stats["cached_batches"] == 0
    assert_partition(result, items)


def test_grouping_schema_version_changes_do_not_touch_extraction_cache(settings, monkeypatch):
    items, store = candidates(2), MaintenanceStore(settings)
    store.put("analysis", "note-one", {"fingerprint": "original", "candidates": items})
    consolidate_topics(Writer(), items, [], store, persist=True)
    monkeypatch.setattr("mynd.connect.grouping.CONSOLIDATION_VERSION", "next-consolidation-version")
    result = consolidate_topics(Writer(), items, [], MaintenanceStore(settings), persist=True)
    assert result.stats["cached_batches"] == 0 and result.stats["attempted_batches"] == 1
    assert MaintenanceStore(settings).get("analysis", "note-one") == {"fingerprint": "original", "candidates": items}


def test_dry_grouping_is_vault_scoped_and_writes_no_state(settings):
    items = candidates(3)
    consolidate_topics(Writer(), items, [], MaintenanceStore(settings))
    assert not settings.database_path.exists() and not settings.data_path.exists()
    consolidate_topics(Writer(), items, [], MaintenanceStore(settings), persist=True)
    other = MaintenanceStore(replace(settings, vault_path=settings.vault_path / "another"))
    assert consolidate_topics(Writer(), items, [], other).stats["cached_batches"] == 0
