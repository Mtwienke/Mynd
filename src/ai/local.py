from __future__ import annotations

import httpx


class OllamaGenerator:
    """Text generator for a locally running Ollama server."""

    def __init__(
        self,
        model: str = "llama3.1",
        base_url: str = "http://localhost:11434",
        timeout: float = 180.0,
    ) -> None:
        self.model = model
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def generate(self, prompt: str) -> str:
        response = self.client.post(
            "/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
        )
        response.raise_for_status()
        result = response.json().get("response", "").strip()
        if not result:
            raise ValueError("Ollama returned an empty response")
        return result

    def close(self) -> None:
        self.client.close()
