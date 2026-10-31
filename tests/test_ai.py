from unittest.mock import MagicMock, patch

import httpx
import pytest

from mynd.ai.base import TextGenerator
from mynd.ai.local import OllamaConfig, OllamaGenerator
from mynd.ai.models import build_text_generator


def test_text_generator_protocol_accepts_concrete_backends():
    assert isinstance(OllamaGenerator(OllamaConfig(model="mistral")), TextGenerator)


def test_text_generator_protocol_rejects_incomplete_backends():
    class Incomplete:
        pass

    assert not isinstance(Incomplete(), TextGenerator)


def test_build_text_generator_returns_local_backend():
    assert isinstance(build_text_generator(), OllamaGenerator)


def test_ollama_generator_is_running_true():
    gen = OllamaGenerator(OllamaConfig(model="mistral"))
    response = MagicMock()
    response.json.return_value = {"models": [{"name": "mistral"}]}
    gen.client = MagicMock()
    gen.client.get.return_value = response
    assert gen.is_running() is True


def test_ollama_generator_is_running_false_for_other_model():
    gen = OllamaGenerator(OllamaConfig(model="mistral"))
    response = MagicMock()
    response.json.return_value = {"models": [{"name": "different"}]}
    gen.client = MagicMock()
    gen.client.get.return_value = response
    assert gen.is_running() is False


def test_ollama_generator_is_running_handles_http_error():
    gen = OllamaGenerator(OllamaConfig(model="mistral"))
    gen.client = MagicMock()
    gen.client.get.side_effect = __import__("httpx").HTTPError("down")
    assert gen.is_running() is False


def test_ollama_generator_ensure_model_already_running():
    gen = OllamaGenerator(OllamaConfig(model="mistral"))
    with patch.object(OllamaGenerator, "is_running", return_value=True):
        assert gen.ensure_model() is True


def test_ollama_generator_ensure_model_pulls_with_timeout():
    gen = OllamaGenerator(OllamaConfig(model="mistral"))
    completed = MagicMock()
    completed.stdout = "pulling\ndone\n"
    completed.stderr = ""
    completed.returncode = 0
    with (
        patch.object(OllamaGenerator, "is_running", side_effect=[False, True]),
        patch("mynd.ai.local.subprocess.run", return_value=completed) as run,
    ):
        assert gen.ensure_model() is True
    run.assert_called_once()
    command = run.call_args.args[0]
    assert command[:2] == ["ollama", "pull"]
    assert command[-1] == "mistral"
    assert run.call_args.kwargs["timeout"] > 0


def test_ollama_generator_ensure_model_reports_failure():
    gen = OllamaGenerator(OllamaConfig(model="mistral"))
    with (
        patch.object(OllamaGenerator, "is_running", return_value=False),
        patch("mynd.ai.local.subprocess.run", side_effect=FileNotFoundError("no ollama")),
    ):
        assert gen.ensure_model() is False


def _stream_context(lines):
    response = MagicMock()
    response.iter_lines.return_value = lines
    context = MagicMock()
    context.__enter__.return_value = response
    context.__exit__.return_value = False
    return context


def test_ollama_generator_generate_collects_stream():
    gen = OllamaGenerator(OllamaConfig(model="mistral"))
    gen.client = MagicMock()
    gen.client.stream.return_value = _stream_context(
        ['{"response": "Hello "}', '{"response": "world", "done": true}']
    )
    assert gen.generate("prompt") == "Hello world"


def test_ollama_generator_generate_empty_raises():
    gen = OllamaGenerator(OllamaConfig(model="mistral"))
    gen.client = MagicMock()
    gen.client.stream.return_value = _stream_context(['{"done": true}'])
    with pytest.raises(ValueError, match="empty response"):
        gen.generate("prompt")


def test_ollama_generator_generate_propagates_error_field():
    gen = OllamaGenerator(OllamaConfig(model="mistral"))
    gen.client = MagicMock()
    gen.client.stream.return_value = _stream_context(['{"error": "boom"}'])
    with pytest.raises(RuntimeError, match="boom"):
        gen.generate("prompt")


def test_read_allocation_reports_the_first_failure():
    gen = OllamaGenerator(OllamaConfig(model="mistral"))
    gen.client = MagicMock()
    gen.client.get.side_effect = httpx.HTTPError("down")

    signature, message = gen._read_allocation()

    assert signature is not None
    assert message is not None and "unavailable" in message


def test_generate_retries_after_a_thermal_interrupt():
    runtime = MagicMock()
    runtime.HOT_STATES = {"serious", "critical"}
    runtime.mode.return_value = "normal"
    runtime.hardware_snapshot.return_value = (None, None)
    runtime.fan_active.return_value = False
    runtime.ollama_options.return_value = {}
    runtime.thermal_state.side_effect = ["serious", "nominal"]
    gen = OllamaGenerator(OllamaConfig(model="mistral"), runtime_control=runtime)
    gen.client = MagicMock()
    gen.client.stream.side_effect = [
        _stream_context(['{"response": "partial"}']),
        _stream_context(['{"response": "final", "done": true}']),
    ]

    assert gen.generate("prompt") == "final"
    assert runtime.wait_until_safe.call_count >= 1
