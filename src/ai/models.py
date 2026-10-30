from __future__ import annotations

from .base import TextGenerator
from .hosted import HostedGenerator
from .local import OllamaGenerator


def get_model_manager(use_local: bool = True, runtime_control=None, progress=None, verbose: bool = False) -> TextGenerator:
    if use_local:
        return OllamaGenerator(runtime_control=runtime_control, progress=progress, verbose=verbose)
    return HostedGenerator()
