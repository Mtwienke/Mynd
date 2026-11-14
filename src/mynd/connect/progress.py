"""Contextual, phase-local progress; percentages count work, never estimate model time."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass


def label(value, limit=220):
    text = " ".join("".join(char if char.isprintable() else " " for char in str(value)).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def names(values, limit=3):
    values = list(dict.fromkeys(str(value) for value in values))
    return "; ".join(label(value, 65) for value in values[:limit]) + (
        f" (+{len(values) - limit} more)" if len(values) > limit else ""
    )


@dataclass
class _Phase:
    step: int
    name: str
    total: int
    unit: str
    done: int = 0
    current: str = ""
    result: str = "Processed"


class ConnectProgress:
    def __init__(self, sink: Callable[[str], None]):
        self.sink = sink
        self.state: _Phase | None = None

    @property
    def step(self):
        return self.state.step if self.state else 0

    def format(self, message):
        state = self.state
        if state is None:
            return "[Connect] " + label(message, 1500)
        percent = state.done * 100 // state.total if state.total else 100
        prefix = (f"[Connect {state.step}/8 | {state.name} {percent}% "
                  f"({state.done}/{state.total} {state.unit})]")
        context = " " + label(state.current) + " —" if state.current else ""
        return prefix + context + " " + label(message, 1500)

    def __call__(self, message):
        self.sink(self.format(message))

    def focus(self, current):
        if self.state:
            self.state.current = current

    def outcome(self, message):
        if self.state:
            self.state.result = message

    def advance(self, count=1, message="Processed", *, emit=True):
        if self.state:
            self.state.done = min(self.state.total, self.state.done + count)
        if emit:
            self(message)

    @contextmanager
    def phase(self, step, name, total, unit):
        previous = self.state
        self.state = _Phase(step, name, total, unit)
        self("Starting" if total else "No work in this phase")
        try:
            yield self
        except BaseException:
            self("Interrupted; phase not completed")
            raise
        else:
            self.state.done = total
            self.state.current = ""
            self("Phase processed")
        finally:
            self.state = previous

    def track(self, step, name, items, unit, describe, *, throttle=False):
        with self.phase(step, name, len(items), unit):
            interval = max(1, (len(items) + 19) // 20) if throttle else 1
            for number, item in enumerate(items, 1):
                self.focus(describe(item))
                self.state.result = "Processed"
                visible = number == 1 or number == len(items) or number % interval == 0
                if visible:
                    self("Processing")
                yield number, item
                self.advance(message=self.state.result, emit=visible)


def progress_for(writer):
    callback = getattr(writer, "progress", None)
    return callback if isinstance(callback, ConnectProgress) else ConnectProgress(callback or (lambda _: None))


@contextmanager
def connect_reporting(writer):
    """Scope writer and verbose runtime messages without changing caller callbacks permanently."""
    missing = object()
    original = getattr(writer, "progress", missing)
    progress = progress_for(writer)
    generator = getattr(writer, "generator", None)
    generator_progress = getattr(generator, "progress", missing)
    writer.progress = progress
    if generator_progress is not missing:
        # Keep SDK users' separate runtime sink, but share the current topic/phase.
        generator.progress = lambda message: generator_progress(progress.format(message))
    try:
        yield progress
    finally:
        if original is missing:
            del writer.progress
        else:
            writer.progress = original
        if generator_progress is not missing:
            generator.progress = generator_progress
