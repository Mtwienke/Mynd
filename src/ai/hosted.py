from __future__ import annotations


class HostedGenerator:
    """Unavailable placeholder for a future hosted model backend."""

    def __init__(self, model: str = "gpt-4") -> None:
        self.model = model

    def is_running(self) -> bool:
        """Hosted generation is not implemented, so it is never available."""
        return False

    def ensure_model(self) -> bool:
        """Hosted generation is not implemented, so it cannot be prepared."""
        return False

    def generate(self, prompt: str) -> str:
        """Refuse to fabricate output for an unimplemented backend."""
        raise NotImplementedError(
            "Hosted text generation is not implemented; "
            "configure a local Ollama model instead."
        )

    def close(self) -> None:
        """No network resources are held by the placeholder backend."""
