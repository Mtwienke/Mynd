from __future__ import annotations

import subprocess
import logging
import httpx
import sys
import json
import time
import threading
from typing import Callable

logger = logging.getLogger(__name__)

class OllamaGenerator:
    """Text generator for a locally running Ollama server."""

    def __init__(
        self,
        model: str = "qwen3:8b",
        base_url: str = "http://localhost:11434",
        timeout: float = 600.0,
        runtime_control=None,
        progress: Callable[[str], None] | None = None,
        verbose: bool = False,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        )
        self.runtime_control = runtime_control
        self.progress = progress or (lambda _message: None)
        self.verbose = verbose
        self._last_runtime_signature = None
        self._last_runtime_error = None

    def _report_request(self, payload: dict) -> None:
        if self.verbose:
            # Deliberately omit the prompt: it may contain the full paper text.
            self.progress(
                "Ollama request: "
                f"model={payload.get('model')}; options="
                f"{json.dumps(payload.get('options', {}), sort_keys=True)}"
            )

    def _report_response(self, stats: dict) -> None:
        if not self.verbose:
            return
        fields = (
            "total_duration", "load_duration", "prompt_eval_count",
            "prompt_eval_duration", "eval_count", "eval_duration",
        )
        details = []
        for key in fields:
            value = stats.get(key)
            if value is not None:
                if key.endswith("_duration"):
                    details.append(f"{key}={value / 1_000_000_000:.2f}s")
                else:
                    details.append(f"{key}={value}")
        eval_count, eval_duration = stats.get("eval_count"), stats.get("eval_duration")
        if isinstance(eval_count, (int, float)) and isinstance(eval_duration, (int, float)) and eval_duration > 0:
            details.append(f"generation_rate={eval_count / (eval_duration / 1_000_000_000):.2f} tokens/s")
        self.progress("Ollama response stats: " + ("; ".join(details) or "no timing stats returned"))

    def _start_runtime_monitor(
        self,
    ) -> tuple[threading.Event, threading.Thread, Callable[[], None]]:
        stop = threading.Event()

        def report_if_changed() -> None:
            try:
                response = self.client.get("/api/ps", timeout=2.0)
                response.raise_for_status()
                models = response.json().get("models", [])
                target = next(
                    (
                        model for model in models
                        if model.get("name") in (self.model, f"{self.model}:latest")
                        or model.get("model") in (self.model, f"{self.model}:latest")
                    ),
                    None,
                )
            except (httpx.HTTPError, ValueError) as exc:
                error = f"could not read Ollama /api/ps: {exc}"
                if error != self._last_runtime_error:
                    self.progress(f"Ollama runtime allocation unavailable: {error}")
                self._last_runtime_error = error
                self._last_runtime_signature = None
                return

            self._last_runtime_error = None
            if target is None:
                signature = ("not-loaded", self.model)
                message = f"Ollama runtime allocation: {self.model} not listed as loaded"
            else:
                size = target.get("size")
                size_vram = target.get("size_vram")
                context_length = target.get("context_length")
                name = target.get("name", self.model)
                signature = (name, size, size_vram, context_length)
                details = [f"name={name}"]
                if isinstance(size, (int, float)) and isinstance(size_vram, (int, float)):
                    percent = (100 * size_vram / size) if size > 0 else None
                    placement = (
                        f"size_vram={size_vram / 1_000_000_000:.2f}GB / "
                        f"size={size / 1_000_000_000:.2f}GB"
                    )
                    if percent is not None:
                        placement += f" (~{percent:.0f}% GPU allocation)"
                    details.append(placement)
                if context_length is not None:
                    details.append(f"context_length={context_length}")
                message = "Ollama runtime allocation (/api/ps): " + "; ".join(details)

            if signature != self._last_runtime_signature:
                self.progress(message)
                self._last_runtime_signature = signature

        def monitor() -> None:
            while not stop.is_set():
                report_if_changed()
                if stop.wait(5.0):
                    return

        thread = threading.Thread(target=monitor, name="ollama-runtime-monitor", daemon=True)
        thread.start()
        return stop, thread, report_if_changed

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
        runtime = self.runtime_control
        while True:
            mode_at_start = None
            if runtime is not None:
                runtime.wait_until_safe(self.progress)
                if runtime.thermal_state() in runtime.HOT_STATES:
                    runtime.wait_until_safe(self.progress)
                    continue
                mode_at_start = runtime.mode()
                sensor_snapshot, _sensor_error = runtime.hardware_snapshot()
                fan_active = mode_at_start == "silent" and runtime.fan_active(sensor_snapshot)
            else:
                fan_active = False

            payload = {"model": self.model, "prompt": prompt, "stream": True}
            options = runtime.ollama_options(mode_at_start, fan_active) if runtime else {}
            if fan_active:
                self.progress(
                    "Fan speed is above 0 RPM; using extra-low inference limits "
                    "(CPU-only request, 1 CPU thread, batch 32) and a 3-second cooldown"
                )
                time.sleep(3)
            if options:
                payload["options"] = options
            self._report_request(payload)

            pieces = []
            interrupted = False
            final_item = {}
            monitor = self._start_runtime_monitor() if runtime and self.verbose else None
            try:
                with self.client.stream("POST", "/api/generate", json=payload) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line:
                            continue
                        item = json.loads(line)
                        if item.get("error"):
                            raise RuntimeError(f"Ollama generation failed: {item['error']}")
                        if item.get("response"):
                            pieces.append(item["response"])
                        if item.get("done"):
                            final_item = item
                            break

                        if runtime is not None:
                            thermal = runtime.thermal_state()
                            if thermal in runtime.HOT_STATES or (
                                thermal == "fair" and mode_at_start != "silent"
                            ):
                                interrupted = True
                                break
            finally:
                if monitor is not None:
                    stop, thread, report_if_changed = monitor
                    stop.set()
                    thread.join(timeout=3.0)
                    report_if_changed()

            if interrupted:
                self.progress("Thermal state changed; restarting this model request with safe limits")
                runtime.wait_until_safe(self.progress)
                continue
            result = "".join(pieces).strip()
            if not result:
                raise ValueError("Ollama returned an empty response")
            self._report_response(final_item)
            if self.verbose and runtime is not None:
                runtime.report_thermal_state("model request complete", self.progress)
            return result

    def close(self) -> None:
        self.client.close()
