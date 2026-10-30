from __future__ import annotations

from collections.abc import Callable

from ai.base import TextGenerator
from ai.hosted import HostedGenerator
from ai.local import OllamaGenerator
from ai.runtime import RuntimeControl


def build_text_generator(
    *,
    use_local: bool = True,
    runtime_control: RuntimeControl | None = None,
    progress: Callable[[str], None] | None = None,
    verbose: bool = False,
) -> TextGenerator:
    """Return the configured text-generation backend."""
    if use_local:
        return OllamaGenerator(
            runtime_control=runtime_control,
            progress=progress,
            verbose=verbose,
        )
    return HostedGenerator()
