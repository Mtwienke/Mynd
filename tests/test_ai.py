from unittest.mock import MagicMock, patch

import pytest

from ai.base import TextGenerator
from ai.hosted import HostedGenerator
from ai.local import OllamaConfig, OllamaGenerator
from ai.models import get_model_manager


def test_text_generator_protocol_accepts_concrete_backends():
    assert isinstance(HostedGenerator(), TextGenerator)


def test_text_generator_protocol_rejects_incomplete_backends():
    class Incomplete:
        pass

    assert not isinstance(Incomplete(), TextGenerator)


def test_get_model_manager_local():
    assert isinstance(get_model_manager(use_local=True), OllamaGenerator)


def test_get_model_manager_hosted():
    assert isinstance(get_model_manager(use_local=False), HostedGenerator)


def test_hosted_generator_stub_behavior():
    manager = HostedGenerator()
    assert manager.is_running() is False
    assert manager.ensure_model() is False
    assert "Hosted generation result" in manager.generate("hi")


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
    with patch.object(
        OllamaGenerator, "is_running", side_effect=[False, True]
    ), patch("ai.local.subprocess.run", return_value=completed) as run:
        assert gen.ensure_model() is True
    run.assert_called_once()
    assert run.call_args.args[0] == ["ollama", "pull", "mistral"]
    assert run.call_args.kwargs["timeout"] == 900


def test_ollama_generator_ensure_model_reports_failure():
    gen = OllamaGenerator(OllamaConfig(model="mistral"))
    with patch.object(OllamaGenerator, "is_running", return_value=False), patch(
        "ai.local.subprocess.run", side_effect=FileNotFoundError("no ollama")
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
