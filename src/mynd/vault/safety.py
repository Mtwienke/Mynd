"""Journaled vault edits with preimages and conservative recovery."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mynd.files import atomic_write_text
from mynd.settings import Settings


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: Path) -> str:
    """Unlike Path.read_text, preserve CRLFs for lossless patches and preimages."""
    return path.read_bytes().decode("utf-8")


def _state_root(settings: Settings) -> Path:
    root = settings.data_path.resolve() / "maintenance"
    if root.is_relative_to(settings.vault_path.resolve()):
        raise ValueError("Maintenance data_path must be outside the Obsidian vault")
    return root / digest(str(settings.vault_path.resolve()))[:16]


@contextmanager
def maintenance_lock(settings: Settings, *, dry_run: bool = False):
    if dry_run:
        yield
        return
    root = _state_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "lock").open("a+b") as handle:
        try:
            import fcntl
        except ImportError:
            import msvcrt
            handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError("Another vault maintenance command is running") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("Another vault maintenance command is running") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass(frozen=True)
class Change:
    path: Path
    before: str | None
    after: str | None


def _check_path(vault: Path, path: Path) -> str:
    absolute = path.absolute()
    root = vault.absolute()
    if not absolute.is_relative_to(root) or not path.resolve().is_relative_to(vault.resolve()):
        raise ValueError(f"Refusing path outside the vault: {path}")
    if any(p.is_symlink() for p in (path, *path.parents) if p != root and p.is_relative_to(root)):
        raise ValueError(f"Refusing symlinked vault path: {path}")
    return absolute.relative_to(root).as_posix()


def _verify(change: Change) -> None:
    current = read_text(change.path) if change.path.exists() else None
    if current != change.before:
        raise ValueError(f"Concurrent edit detected; refusing to overwrite {change.path}")


def apply_changes(settings: Settings, changes: list[Change], operation: str, *,
                  dependencies: dict[Path, str] | None = None,
                  on_written: Callable[[Path], None] | None = None) -> str | None:
    changes = [change for change in changes if change.before != change.after]
    if not changes:
        return None
    paths = [_check_path(settings.vault_path, change.path) for change in changes]
    if len(paths) != len(set(paths)):
        raise ValueError("Duplicate paths in a maintenance change set")
    for change in changes:
        _verify(change)
    # An unchanged source may still feed a newly rendered topic. Check those read
    # dependencies too, not only files that happen to receive an edit this run.
    for path, content in (dependencies or {}).items():
        _check_path(settings.vault_path, path)
        _verify(Change(path, content, content))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12]
    root = _state_root(settings) / run_id
    root.mkdir(parents=True)
    entries = []
    for index, (relative, change) in enumerate(zip(paths, changes)):
        if change.before is not None:
            atomic_write_text(root / f"{index}.before", change.before)
        entries.append({"path": relative, "before": digest(change.before)
                        if change.before is not None else None,
                        "after": digest(change.after) if change.after is not None else None,
                        "written": False})
    manifest = {"vault": str(settings.vault_path.resolve()), "operation": operation,
                "status": "prepared", "entries": entries}

    def save():
        atomic_write_text(root / "manifest.json", json.dumps(manifest, indent=2))

    save()
    # Filesystem and SQLite cannot share a transaction. The journal is durable before
    # the first edit; recovery checks postimages, never blindly restores over new work.
    for change, entry in zip(changes, entries):
        _verify(change)
        if change.after is None:
            change.path.unlink()  # Checked preimage, backed up before deletion/move.
        else:
            atomic_write_text(change.path, change.after)
        entry["written"] = True
        save()
        if on_written:
            on_written(change.path)
    manifest["status"] = "complete"
    save()
    return run_id


def restore_run(settings: Settings, run_id: str) -> list[str]:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("Invalid maintenance run ID")
    root = _state_root(settings) / run_id
    manifest = json.loads(read_text(root / "manifest.json"))
    if manifest["vault"] != str(settings.vault_path.resolve()):
        raise ValueError("Backup belongs to another vault")
    pending = []
    for index, entry in enumerate(manifest["entries"]):
        path = settings.vault_path / entry["path"]
        _check_path(settings.vault_path, path)
        current = read_text(path) if path.exists() else None
        current_hash = digest(current) if current is not None else None
        if current_hash == entry["before"]:
            continue
        if current_hash != entry["after"]:
            raise ValueError(f"Cannot restore: {path} was edited after this run")
        before = read_text(root / f"{index}.before") if entry["before"] else None
        if before is not None and digest(before) != entry["before"]:
            raise ValueError(f"Damaged backup for {path}")
        pending.append((path, current, before))
    for path, current, before in reversed(pending):
        if (read_text(path) if path.exists() else None) != current:
            raise ValueError(f"Concurrent edit detected during recovery: {path}")
        if before is None:
            path.unlink()  # Only this run's own, unchanged new file.
        else:
            atomic_write_text(path, before)
    manifest["status"] = "restored"
    atomic_write_text(root / "manifest.json", json.dumps(manifest, indent=2))
    return [str(path) for path, _, _ in pending]
