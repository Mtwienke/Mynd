from typing import Protocol


class TextGenerator(Protocol):
    """A model backend that turns a prompt into a text completion."""

    def generate(self, prompt: str) -> str:
        ...
