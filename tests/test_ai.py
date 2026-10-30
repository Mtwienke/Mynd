from unittest.mock import MagicMock, patch

import pytest

from ai.hosted import HostedGenerator
from ai.local import OllamaGenerator
from ai.models import get_model_manager


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
    gen = OllamaGenerator(model="mistral")
    response = MagicMock()
    response.json.return_value = {"models": [{"name": "mistral"}]}
    gen.client = MagicMock()
    gen.client.get.return_value = response
    assert gen.is_running() is True


def test_ollama_generator_is_running_false_for_other_model():
    gen = OllamaGenerator(model="mistral")
    response = MagicMock()
    response.json.return_value = {"models": [{"name": "different"}]}
    gen.client = MagicMock()
    gen.client.get.return_value = response
    assert gen.is_running() is False


def test_ollama_generator_is_running_handles_http_error():
    gen = OllamaGenerator(model="mistral")
    gen.client = MagicMock()
    gen.client.get.side_effect = __import__("httpx").HTTPError("down")
    assert gen.is_running() is False


def test_ollama_generator_ensure_model_already_running():
    gen = OllamaGenerator(model="mistral")
    with patch.object(OllamaGenerator, "is_running", return_value=True):
        assert gen.ensure_model() is True


def test_ollama_generator_ensure_model_pulls_via_popen():
    gen = OllamaGenerator(model="mistral")
    process = MagicMock()
    process.stdout = ["pulling\n", "done\n"]
    process.returncode = 0
    with patch.object(
        OllamaGenerator, "is_running", side_effect=[False, True]
    ), patch("ai.local.subprocess.Popen", return_value=process) as popen:
        assert gen.ensure_model() is True
    popen.assert_called_once()
    assert popen.call_args.args[0] == ["ollama", "pull", "mistral"]


def test_ollama_generator_ensure_model_reports_failure():
    gen = OllamaGenerator(model="mistral")
    with patch.object(OllamaGenerator, "is_running", return_value=False), patch(
        "ai.local.subprocess.Popen", side_effect=FileNotFoundError("no ollama")
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
    gen = OllamaGenerator(model="mistral")
    gen.client = MagicMock()
    gen.client.stream.return_value = _stream_context(
        ['{"response": "Hello "}', '{"response": "world", "done": true}']
    )
    assert gen.generate("prompt") == "Hello world"


def test_ollama_generator_generate_empty_raises():
    gen = OllamaGenerator(model="mistral")
    gen.client = MagicMock()
    gen.client.stream.return_value = _stream_context(['{"done": true}'])
    with pytest.raises(ValueError, match="empty response"):
        gen.generate("prompt")


def test_ollama_generator_generate_propagates_error_field():
    gen = OllamaGenerator(model="mistral")
    gen.client = MagicMock()
    gen.client.stream.return_value = _stream_context(['{"error": "boom"}'])
    with pytest.raises(RuntimeError, match="boom"):
        gen.generate("prompt")
