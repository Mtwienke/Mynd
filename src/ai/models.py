from __future__ import annotations

from collections.abc import Callable

from ai.base import TextGenerator
from ai.local import OllamaGenerator
from ai.runtime import RuntimeControl


def build_text_generator(
    *,
    runtime_control: RuntimeControl | None = None,
    progress: Callable[[str], None] | None = None,
    verbose: bool = False,
) -> TextGenerator:
    """Return the configured text-generation backend."""
    return OllamaGenerator(
        runtime_control=runtime_control,
        progress=progress,
        verbose=verbose,
    )
