from typing import Protocol, runtime_checkable


@runtime_checkable
class TextGenerator(Protocol):
    """A model backend that turns a prompt into a text completion."""

    def generate(self, prompt: str) -> str:
        ...

    def is_running(self) -> bool:
        """Check if the model backend is available."""
        ...

    def ensure_model(self) -> bool:
        """Ensure the model is available and ready for use."""
        ...
