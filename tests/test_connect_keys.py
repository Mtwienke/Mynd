import json
import os
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mynd.cli import app
from mynd.connect.web import OllamaWeb


@pytest.fixture
def connect_cli(config_file, monkeypatch):
    # Keep any real keys out of these tests and restore all environment changes.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for name in ("OLLAMA_API_KEY", "MYND_TEST_API_KEY", "PYTHON_DOTENV_DISABLED"):
        monkeypatch.delenv(name, raising=False)
    coordinator = MagicMock(return_value={"status": "complete"})
    generator = MagicMock()
    monkeypatch.setattr("mynd.cli.connect_vault", coordinator)
    monkeypatch.setattr("mynd.cli.build_text_generator", MagicMock(return_value=generator))

    def run(*flags):
        return CliRunner().invoke(
            app, ["connect", "--config", str(config_file), "--non-interactive", "--json", *flags]
        )

    return run, coordinator, generator


@pytest.mark.parametrize(("flags", "configured"), [
    (["--web"], False),
    (["--refresh-web"], False),
    ([], True),
])
def test_connect_loads_keys_next_to_selected_config(
    connect_cli, config_file, tmp_path, monkeypatch, flags, configured
):
    run, coordinator, generator = connect_cli
    if configured:
        with config_file.open("a", encoding="utf-8") as config:
            config.write("topic_web_enabled = true\n")
    (tmp_path / ".keys").write_text(
        "# Private keys\nexport OLLAMA_API_KEY='fake-file-key'\n"
        "export MYND_TEST_API_KEY='another fake key'\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".keys").write_text("OLLAMA_API_KEY=wrong-directory\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    captured = []

    def connect(*args, **kwargs):
        web = OllamaWeb(client=MagicMock())
        captured.append(web.api_key)
        web.close()
        return {"status": "complete"}

    coordinator.side_effect = connect
    result = run(*flags)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"status": "complete"}
    assert captured == ["fake-file-key"]
    assert os.environ["MYND_TEST_API_KEY"] == "another fake key"
    assert "fake-file-key" not in result.output
    assert "another fake key" not in result.output
    generator.close.assert_called_once()


def test_connect_preserves_environment_keys_and_active_venv(connect_cli, tmp_path, monkeypatch):
    run, _, _ = connect_cli
    existing = {
        "OLLAMA_API_KEY": "fake-environment-key",
        "VIRTUAL_ENV": "/existing/venv",
        "PATH": "/existing/venv/bin:/usr/bin",
    }
    for name, value in existing.items():
        monkeypatch.setenv(name, value)
    (tmp_path / ".keys").write_text(
        "export OLLAMA_API_KEY='fake-file-key'\n"
        "export VIRTUAL_ENV='/other/venv'\n"
        "export PATH='/other/bin'\n",
        encoding="utf-8",
    )

    result = run("--web")

    assert result.exit_code == 0, result.output
    assert {name: os.environ[name] for name in existing} == existing


@pytest.mark.parametrize("directory", [False, True])
def test_connect_tolerates_missing_keys_file(connect_cli, tmp_path, directory):
    run, coordinator, _ = connect_cli
    if directory:
        (tmp_path / ".keys").mkdir()

    result = run("--web")

    assert result.exit_code == 0, result.output
    coordinator.assert_called_once()
    assert "OLLAMA_API_KEY" not in os.environ


@pytest.mark.parametrize(("flags", "configured"), [
    ([], False),
    (["--offline"], True),
])
def test_connect_skips_keys_without_web(connect_cli, config_file, tmp_path, flags, configured):
    run, coordinator, _ = connect_cli
    if configured:
        with config_file.open("a", encoding="utf-8") as config:
            config.write("topic_web_enabled = true\n")
    (tmp_path / ".keys").write_text("export OLLAMA_API_KEY='fake-file-key'\n", encoding="utf-8")

    result = run(*flags)

    assert result.exit_code == 0, result.output
    coordinator.assert_called_once()
    assert "OLLAMA_API_KEY" not in os.environ


def test_connect_loads_literal_values_without_shell_execution(connect_cli, tmp_path, monkeypatch):
    run, _, _ = connect_cli
    marker = tmp_path / "must-not-exist"
    monkeypatch.setenv("MYND_TEST_VARIABLE", "must-not-expand")
    command = f"$(touch {marker})"
    (tmp_path / ".keys").write_text(
        f"export OLLAMA_API_KEY='{command}'\n"
        "export MYND_TEST_API_KEY='${MYND_TEST_VARIABLE}'\n",
        encoding="utf-8",
    )

    result = run("--web")

    assert result.exit_code == 0, result.output
    assert os.environ["OLLAMA_API_KEY"] == command
    assert os.environ["MYND_TEST_API_KEY"] == "${MYND_TEST_VARIABLE}"
    assert not marker.exists()


def test_connect_reports_unreadable_keys_and_closes_generator(connect_cli, tmp_path, monkeypatch):
    run, coordinator, generator = connect_cli
    (tmp_path / ".keys").touch()
    loader = MagicMock(side_effect=PermissionError("Cannot read .keys"))
    monkeypatch.setattr("mynd.cli.load_dotenv", loader)

    result = run("--web")

    assert result.exit_code == 1
    assert "Could not connect vault: Cannot read .keys" in result.stderr
    coordinator.assert_not_called()
    generator.close.assert_called_once()


def test_connect_forwards_repeatable_public_query_corrections(connect_cli):
    run, coordinator, generator = connect_cli
    result = run("--web", "--web-query", "topic-one=distributed optimization",
                 "--web-query", "topic-two=metaheuristics x=y")
    assert result.exit_code == 0, result.output
    assert coordinator.call_args.kwargs["web_query_overrides"] == {
        "topic-one": "distributed optimization", "topic-two": "metaheuristics x=y",
    }
    generator.close.assert_called_once()


@pytest.mark.parametrize("value", ["missing-separator", "=missing-topic", "topic-one="])
def test_connect_rejects_malformed_public_query_option(connect_cli, value):
    run, coordinator, generator = connect_cli
    result = run("--web", "--web-query", value)
    assert result.exit_code == 1
    assert "Use --web-query TOPIC_ID=PUBLIC_QUERY" in result.stderr
    coordinator.assert_not_called()
    generator.close.assert_called_once()
