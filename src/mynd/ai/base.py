from typing import Protocol, runtime_checkable


@runtime_checkable
class TextGenerator(Protocol):
    """A model backend that turns a prompt into a text completion."""

    model: str

    def generate(self, prompt: str) -> str:
        ...

    def is_running(self) -> bool:
        """Check if the model backend is available."""
        ...

    def ensure_model(self) -> bool:
        """Ensure the model is available and ready for use."""
        ...

    def close(self) -> None:
        """Release any network resources held by the backend."""
        ...


@runtime_checkable
class StructuredTextGenerator(Protocol):
    """Optional capability; ordinary paper generation keeps its existing contract."""

    def generate_structured(self, prompt: str, schema: dict) -> str:
        ...
