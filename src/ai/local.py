from __future__ import annotations

import subprocess
import logging
import httpx
import sys

logger = logging.getLogger(__name__)

class OllamaGenerator:
    """Text generator for a locally running Ollama server."""

    def __init__(
        self,
        model: str = "qwen3:8b",
        base_url: str = "http://localhost:11434",
        timeout: float = 600.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        )

    def is_running(self) -> bool:
        """Check if Ollama is running and the model is available."""
        try:
            response = self.client.get("/api/tags", timeout=2.0)
            response.raise_for_status()
            models = response.json().get("models", [])
            return any(m.get("name") == self.model or m.get("name") == f"{self.model}:latest" 
                       for m in models)
        except (httpx.HTTPError, ValueError):
            return False

    def ensure_model(self) -> bool:
        """Ensure Ollama is running and the model is pulled."""
        if self.is_running():
            return True

        print(
            f"[ai] Model {self.model} is not available from Ollama; running `ollama pull {self.model}`. "
            "Ollama's layer download progress follows:",
            file=sys.stderr,
            flush=True,
        )
        try:
            # Use Popen to stream output from 'ollama pull' to the terminal
            process = subprocess.Popen(
                ["ollama", "pull", self.model],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            for line in process.stdout:
                print(f"  [Ollama] {line.rstrip()}", file=sys.stderr, flush=True)
            
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, ["ollama", "pull", self.model])
            
            return self.is_running()
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.error("Failed to pull Ollama model %s: %s", self.model, e)
            print(f"[ai] Failed to download model {self.model}: {e}", file=sys.stderr, flush=True)
            return False

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
