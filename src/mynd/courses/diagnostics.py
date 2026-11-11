"""Verbose stage diagnostics without logging slide text, prompts, or model responses."""
from __future__ import annotations

import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import monotonic


class Diagnostics:
    def __init__(self, enabled: bool, progress: Callable[[str], None]):
        self.enabled = enabled
        self.progress = progress
        self.failed_stage = ""

    def log(self, message: str) -> None:
        if self.enabled:
            self.progress(f"[debug] {message}")

    def failure(self, name: str, exc: Exception, elapsed: float | None = None) -> None:
        self.failed_stage = name
        duration = f" after {elapsed:.2f}s" if elapsed is not None else ""
        self.log(f"stage={name} failed{duration}: {type(exc).__name__}: {exc}")
        if self.enabled:
            self.log(traceback.format_exc().rstrip())

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        started = monotonic()
        self.log(f"stage={name} start")
        try:
            yield
        except Exception as exc:
            self.failure(name, exc, monotonic() - started)
            raise
        else:
            self.log(f"stage={name} complete in {monotonic() - started:.2f}s")
