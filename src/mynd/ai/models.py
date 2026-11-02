from __future__ import annotations

from collections.abc import Callable

from mynd.ai.base import TextGenerator
from mynd.ai.local import OllamaGenerator
from mynd.ai.runtime import RuntimeControl


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
