import pytest
from unittest.mock import MagicMock, patch
from src.ai.models import get_model_manager
from src.ai.local import OllamaGenerator
from src.ai.hosted import HostedGenerator

def test_get_model_manager_local():
    manager = get_model_manager(use_local=True)
    assert isinstance(manager, OllamaGenerator)

def test_get_model_manager_hosted():
    manager = get_model_manager(use_local=False)
    assert isinstance(manager, HostedGenerator)

def test_hosted_generator_defaults():
    manager = HostedGenerator()
    # According to current implementation, hosted should return False for debugging
    assert manager.is_running() is False
    assert manager.ensure_model() is False

@patch("src.ai.local.httpx.Client.get")
def test_ollama_generator_is_running_true(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"models": [{"name": "mistral"}]}
    mock_get.return_value = mock_response

    gen = OllamaGenerator(model="mistral")
    assert gen.is_running() is True

@patch("src.ai.local.httpx.Client.get")
def test_ollama_generator_is_running_false(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"models": [{"name": "some-other-model"}]}
    mock_get.return_value = mock_response

    gen = OllamaGenerator(model="mistral")
    assert gen.is_running() is False

@patch("src.ai.local.subprocess.run")
@patch("src.ai.local.OllamaGenerator.is_running")
def test_ollama_generator_ensure_model_pulls(mock_is_running, mock_run):
    # First call False (not running), second call True (after pull)
    mock_is_running.side_effect = [False, True]
    
    gen = OllamaGenerator(model="mistral")
    result = gen.ensure_model()
    
    assert result is True
    mock_run.assert_called_once_with(["ollama", "pull", "mistral"], check=True, capture_output=True)

@patch("src.ai.local.subprocess.run")
@patch("src.ai.local.OllamaGenerator.is_running")
def test_ollama_generator_ensure_model_fails(mock_is_running, mock_run):
    mock_is_running.return_value = False
    mock_run.side_effect = FileNotFoundError("ollama not found")
    
    gen = OllamaGenerator(model="mistral")
    result = gen.ensure_model()
    
    assert result is False
