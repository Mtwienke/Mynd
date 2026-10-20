from __future__ import annotations

from .local import OllamaGenerator
from .hosted import HostedGenerator
from .base import TextGenerator

def get_model_manager(use_local: bool = True) -> TextGenerator:
    if use_local:
        return OllamaGenerator()
    return HostedGenerator()
