import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from mynd.cli import _grobid_health_url, _wait_for_grobid, app
from mynd.questionnize import QuestionSet
from mynd.settings import Settings

runner = CliRunner()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        vault_path=tmp_path / "vault",
        data_path=tmp_path / "data",
        inbox_path=tmp_path / "inbox",
        pdf_store_path=tmp_path / "pdfs",
        database_path=tmp_path / "library.sqlite3",
        use_grobid=False,
    )


def test_settings_from_missing_config_errors(tmp_path):
    result = runner.invoke(app, ["doctor", "--config", str(tmp_path / "missing.toml")])
    assert result.exit_code != 0
    assert "config" in result.output.lower()


def test_doctor_creates_directories(tmp_path, config_file):
    result = runner.invoke(app, ["doctor", "--config", str(config_file)])
    assert result.exit_code == 0
    assert (tmp_path / "vault").is_dir()
    assert (tmp_path / "inbox").is_dir()
    assert str(tmp_path / "vault") in result.output or "vault" in result.output.lower()


def test_mode_command_sets_mode(tmp_path, config_file):
    result = runner.invoke(app, ["mode", "silent", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "silent" in result.output
    assert (tmp_path / "data" / ".mynd-mode").read_text(encoding="utf-8").strip() == "silent"


def test_mode_command_rejects_invalid_mode(config_file):
    result = runner.invoke(app, ["mode", "turbo", "--config", str(config_file)])
    assert result.exit_code != 0


def test_ingest_reports_results_and_closes_client(tmp_path, config_file):
    settings = _settings(tmp_path)
    fake_client = MagicMock()
    with (
        patch("mynd.cli.load_settings", return_value=settings),
        patch("mynd.cli.CrossrefClient", return_value=fake_client),
        patch(
            "mynd.cli.ingest_inbox",
            return_value=[{"status": "ingested", "paper_id": "paper-1"}],
        ),
    ):
        result = runner.invoke(app, ["ingest", "--config", str(config_file)])
    assert result.exit_code == 0
    assert json.loads(result.output.strip().splitlines()[-1])["status"] == "ingested"
    fake_client.close.assert_called_once()


def test_ingest_without_pdfs_reports_empty(tmp_path, config_file):
    settings = _settings(tmp_path)
    with (
        patch("mynd.cli.load_settings", return_value=settings),
        patch("mynd.cli.CrossrefClient", return_value=MagicMock()),
        patch("mynd.cli.ingest_inbox", return_value=[]),
    ):
        result = runner.invoke(app, ["ingest", "--config", str(config_file)])
    assert result.exit_code == 0
    assert result.output.strip()


def test_review_queue_command_prints_path(tmp_path, config_file):
    settings = _settings(tmp_path)
    expected = tmp_path / "vault" / "review.md"
    with (
        patch("mynd.cli.load_settings", return_value=settings),
        patch("mynd.cli.write_review_queue", return_value=expected),
    ):
        result = runner.invoke(app, ["review-queue", "--config", str(config_file)])
    assert result.exit_code == 0
    assert str(expected) in result.output


def _questions() -> QuestionSet:
    return QuestionSet(
        main_idea=("One?", "Two?", "Three?"),
        methodology=("One?", "Two?", "Three?", "Four?"),
        findings=("One?", "Two?", "Three?"),
    )


def test_questionnize_command_writes_questions(tmp_path, config_file):
    settings = _settings(tmp_path)
    note = tmp_path / "vault" / "10 Papers" / "Paper.md"
    note.parent.mkdir(parents=True)
    note.write_text("## Summary\n\nA summary.\n", encoding="utf-8")
    generator = MagicMock()
    generator.ensure_model.return_value = True
    generator.model = "fake-model"
    with (
        patch("mynd.cli.load_settings", return_value=settings),
        patch("mynd.cli.build_text_generator", return_value=generator),
        patch("mynd.cli.questionnize_note", return_value=_questions()),
    ):
        result = runner.invoke(
            app, ["questionnize", str(note), "--config", str(config_file)]
        )

    assert result.exit_code == 0
    assert "Wrote 10 questions" in result.output
    generator.close.assert_called_once()


def test_questionnize_command_reports_missing_model(tmp_path, config_file):
    settings = _settings(tmp_path)
    note = tmp_path / "note.md"
    note.write_text("## Summary\n\nA summary.\n", encoding="utf-8")
    generator = MagicMock()
    generator.ensure_model.return_value = False
    generator.model = "fake-model"
    with (
        patch("mynd.cli.load_settings", return_value=settings),
        patch("mynd.cli.build_text_generator", return_value=generator),
    ):
        result = runner.invoke(
            app, ["questionnize", str(note), "--config", str(config_file)]
        )

    assert result.exit_code == 1
    assert "unavailable" in result.output
    generator.close.assert_called_once()


def test_settings_from_reports_malformed_config(tmp_path):
    config = tmp_path / "bad.toml"
    config.write_text("this is not toml", encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--config", str(config)])

    assert result.exit_code != 0
    assert "config" in result.output.lower()


def test_settings_from_reports_missing_required_keys(tmp_path):
    config = tmp_path / "partial.toml"
    config.write_text('vault_path = "vault"\n', encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--config", str(config)])

    assert result.exit_code != 0
    assert "database_path" in result.output


def test_grobid_health_url_uses_isalive_endpoint():
    assert (
        _grobid_health_url("http://localhost:8070/")
        == "http://localhost:8070/api/isalive"
    )


def test_wait_for_grobid_polls_health_endpoint():
    response = MagicMock()
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.return_value = response

    with patch("mynd.cli.httpx.Client", return_value=client):
        assert _wait_for_grobid("http://localhost:8070", timeout=0.1, interval=0)

    client.get.assert_called_once_with(
        "http://localhost:8070/api/isalive", timeout=2.0
    )
