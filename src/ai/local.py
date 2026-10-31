from __future__ import annotations

import json
import logging
import subprocess  # nosec B404 - used only for fixed, trusted local CLI tools (ollama)
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from mynd.contracts import InferenceMode

if TYPE_CHECKING:
    from ai.runtime import RuntimeControl

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:8b"
DEFAULT_TIMEOUT = 600.0
PULL_TIMEOUT_SECONDS = 900
MONITOR_INTERVAL_SECONDS = 5.0
MONITOR_JOIN_SECONDS = 3.0
FAN_COOLDOWN_SECONDS = 3
STATUS_TIMEOUT_SECONDS = 2.0
NANOSECONDS_PER_SECOND = 1_000_000_000
BYTES_PER_GIGABYTE = 1_000_000_000
THERMAL_CHECK_INTERVAL_SECONDS = 0.5
FAN_LIMITS_MESSAGE = (
    "Fan speed is above 0 RPM; using extra-low inference limits "
    "(CPU-only request, 1 CPU thread, batch 32) and a 3-second cooldown"
)
_RESPONSE_STAT_FIELDS = (
    "total_duration",
    "load_duration",
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
)


@dataclass(frozen=True)
class OllamaConfig:
    """Connection settings for a local Ollama server."""

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_OLLAMA_URL
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class _Attempt:
    """Thermal context captured before one model request is sent."""

    mode: InferenceMode | None
    fan_active: bool


class OllamaGenerator:
    """Text generator for a locally running Ollama server."""

    def __init__(
        self,
        config: OllamaConfig | None = None,
        *,
        runtime_control: RuntimeControl | None = None,
        progress: Callable[[str], None] | None = None,
        verbose: bool = False,
    ) -> None:
        self.config = config or OllamaConfig()
        self.runtime_control = runtime_control
        self.progress = progress or (lambda _message: None)
        self.verbose = verbose
        self.client = httpx.Client(
            base_url=self.config.base_url.rstrip("/"),
            timeout=httpx.Timeout(
                self.config.timeout, connect=min(self.config.timeout, 10.0)
            ),
        )
        self._last_runtime_signature: tuple | None = None
        self._last_runtime_error: str | None = None

    @property
    def model(self) -> str:
        """Return the configured model name."""
        return self.config.model

    def _report_request(self, payload: dict) -> None:
        if not self.verbose:
            return
        # Deliberately omit the prompt: it may contain the full paper text.
        self.progress(
            "Ollama request: "
            f"model={payload.get('model')}; options="
            f"{json.dumps(payload.get('options', {}), sort_keys=True)}"
        )

    def _report_response(self, stats: dict) -> None:
        if not self.verbose:
            return
        details = []
        for key in _RESPONSE_STAT_FIELDS:
            value = stats.get(key)
            if value is None:
                continue
            if key.endswith("_duration"):
                details.append(f"{key}={value / NANOSECONDS_PER_SECOND:.2f}s")
            else:
                details.append(f"{key}={value}")
        rate = _tokens_per_second(stats)
        if rate is not None:
            details.append(f"generation_rate={rate:.2f} tokens/s")
        self.progress(
            "Ollama response stats: " + ("; ".join(details) or "no timing stats returned")
        )

    def _start_runtime_monitor(
        self,
    ) -> tuple[threading.Event, threading.Thread, Callable[[], None]]:
        stop = threading.Event()

        def report_if_changed() -> None:
            signature, message = self._read_allocation()
            if signature == self._last_runtime_signature:
                return
            if message is not None:
                self.progress(message)
            self._last_runtime_signature = signature

        def monitor() -> None:
            while not stop.is_set():
                report_if_changed()
                if stop.wait(MONITOR_INTERVAL_SECONDS):
                    return

        thread = threading.Thread(target=monitor, name="ollama-runtime-monitor", daemon=True)
        thread.start()
        return stop, thread, report_if_changed

    def _read_allocation(self) -> tuple[tuple | None, str | None]:
        """Read ``/api/ps`` once, returning a change signature and optional message."""
        try:
            response = self.client.get("/api/ps", timeout=STATUS_TIMEOUT_SECONDS)
            response.raise_for_status()
            models = response.json().get("models", [])
        except (httpx.HTTPError, ValueError) as exc:
            error = f"could not read Ollama /api/ps: {exc}"
            message = None
            if error != self._last_runtime_error:
                message = f"Ollama runtime allocation unavailable: {error}"
            self._last_runtime_error = error
            return ("unavailable", error), message

        self._last_runtime_error = None
        target = _loaded_model(models, self.model)
        if target is None:
            message = f"Ollama runtime allocation: {self.model} not listed as loaded"
            return ("not-loaded", self.model), message
        return _allocation_signature(target), _allocation_message(target, self.model)

    def _stop_monitor(
        self, monitor: tuple[threading.Event, threading.Thread, Callable[[], None]]
    ) -> None:
        stop, thread, report_if_changed = monitor
        stop.set()
        thread.join(timeout=MONITOR_JOIN_SECONDS)
        report_if_changed()

    def is_running(self) -> bool:
        """Check if Ollama is running and the model is available."""
        try:
            response = self.client.get("/api/tags", timeout=STATUS_TIMEOUT_SECONDS)
            response.raise_for_status()
            models = response.json().get("models", [])
        except (httpx.HTTPError, ValueError) as exc:
            self._last_runtime_error = f"ollama /api/tags unavailable: {exc}"
            logger.warning("Cannot check Ollama model availability: %s", exc)
            return False
        return _loaded_model(models, self.model) is not None

    def ensure_model(self) -> bool:
        """Ensure Ollama is running and the model is pulled."""
        if self.is_running():
            return True

        self.progress(
            f"Model {self.model} is not available from Ollama; running "
            f"`ollama pull {self.model}`. Ollama's layer download progress follows:"
        )
        try:
            # The executable and arguments are fixed, trusted values, never
            # shell-interpreted user input. A bounded timeout prevents a stuck
            # download from hanging ingestion indefinitely.
            result = subprocess.run(  # nosec B603 B607 - fixed local binary, no shell
                ["ollama", "pull", self.model],
                capture_output=True,
                text=True,
                timeout=PULL_TIMEOUT_SECONDS,
                check=False,
            )
            for line in (result.stdout or "").splitlines():
                self.progress(f"Ollama pull: {line}")
            if result.returncode != 0:
                detail = (result.stderr or "").strip() or f"exit code {result.returncode}"
                logger.error("Failed to pull Ollama model %s: %s", self.model, detail)
                self.progress(f"Failed to download model {self.model}: {detail}")
                return False
            return self.is_running()
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.error("Failed to pull Ollama model %s: %s", self.model, exc)
            self.progress(f"Failed to download model {self.model}: {exc}")
            return False

    def generate(self, prompt: str) -> str:
        """Generate a completion, retrying with safe limits if thermals change."""
        runtime = self.runtime_control
        while True:
            attempt = self._prepare_attempt(runtime)
            pieces, final_item, interrupted = self._run_request(prompt, attempt)
            if interrupted:
                self.progress(
                    "Thermal state changed; restarting this model request with safe limits"
                )
                continue
            result = "".join(pieces).strip()
            if not result:
                raise ValueError("Ollama returned an empty response")
            self._report_response(final_item)
            if self.verbose and runtime is not None:
                runtime.report_thermal_state("model request complete", self.progress)
            return result

    def _prepare_attempt(self, runtime: RuntimeControl | None) -> _Attempt:
        """Wait for a safe thermal state and capture the mode for this request."""
        if runtime is None:
            return _Attempt(mode=None, fan_active=False)
        runtime.wait_until_safe(self.progress)
        mode = runtime.mode()
        snapshot, _error = runtime.hardware_snapshot()
        fan_active = mode == "silent" and runtime.fan_active(snapshot)
        return _Attempt(mode=mode, fan_active=fan_active)

    def _run_request(
        self, prompt: str, attempt: _Attempt
    ) -> tuple[list[str], dict, bool]:
        """Send one request, streaming pieces and reporting runtime allocation."""
        runtime = self.runtime_control
        payload: dict = {"model": self.model, "prompt": prompt, "stream": True}
        options = runtime.ollama_options(attempt.mode, attempt.fan_active) if runtime else {}
        if options:
            payload["options"] = options
        if attempt.fan_active:
            self.progress(FAN_LIMITS_MESSAGE)
            time.sleep(FAN_COOLDOWN_SECONDS)
        self._report_request(payload)

        monitor = self._start_runtime_monitor() if runtime and self.verbose else None
        try:
            pieces, final_item, interrupted = self._consume_stream(payload)
        finally:
            if monitor is not None:
                self._stop_monitor(monitor)
        return pieces, final_item, interrupted

    def _consume_stream(self, payload: dict) -> tuple[list[str], dict, bool]:
        """Read the streaming response, stopping early when thermals degrade."""
        pieces: list[str] = []
        final_item: dict = {}
        next_thermal_check = 0.0
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
                now = time.monotonic()
                if now >= next_thermal_check:
                    next_thermal_check = now + THERMAL_CHECK_INTERVAL_SECONDS
                    if self._thermal_interrupt():
                        return pieces, final_item, True
        return pieces, final_item, False

    def _thermal_interrupt(self) -> bool:
        """Return True when the runtime reports a thermal state that must pause."""
        runtime = self.runtime_control
        return runtime is not None and runtime.thermal_state() in runtime.HOT_STATES

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self.client.close()


def _loaded_model(models: list[dict], model: str) -> dict | None:
    """Return the entry describing ``model`` in an Ollama model listing."""
    names = (model, f"{model}:latest")
    for candidate in models:
        if candidate.get("name") in names or candidate.get("model") in names:
            return candidate
    return None


def _allocation_signature(target: dict) -> tuple:
    return (
        target.get("name", ""),
        target.get("size"),
        target.get("size_vram"),
        target.get("context_length"),
    )


def _allocation_message(target: dict, model: str) -> str:
    name = target.get("name", model)
    details = [f"name={name}"]
    size, size_vram = target.get("size"), target.get("size_vram")
    if isinstance(size, (int, float)) and isinstance(size_vram, (int, float)):
        details.append(_vram_placement(size, size_vram))
    context_length = target.get("context_length")
    if context_length is not None:
        details.append(f"context_length={context_length}")
    return "Ollama runtime allocation (/api/ps): " + "; ".join(details)


def _vram_placement(size: float, size_vram: float) -> str:
    placement = (
        f"size_vram={size_vram / BYTES_PER_GIGABYTE:.2f}GB / size={size / BYTES_PER_GIGABYTE:.2f}GB"
    )
    if size <= 0:
        return placement
    return placement + f" (~{100 * size_vram / size:.0f}% GPU allocation)"


def _tokens_per_second(stats: dict) -> float | None:
    count, duration = stats.get("eval_count"), stats.get("eval_duration")
    if not isinstance(count, (int, float)) or not isinstance(duration, (int, float)):
        return None
    if duration <= 0:
        return None
    return count / (duration / NANOSECONDS_PER_SECOND)
