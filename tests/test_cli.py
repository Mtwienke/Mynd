import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from mynd.cli import app
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


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                f'vault_path = "{(tmp_path / "vault").as_posix()}"',
                f'data_path = "{(tmp_path / "data").as_posix()}"',
                f'inbox_path = "{(tmp_path / "inbox").as_posix()}"',
                f'pdf_store_path = "{(tmp_path / "pdfs").as_posix()}"',
                f'database_path = "{(tmp_path / "library.sqlite3").as_posix()}"',
                "use_grobid = false",
            ]
        ),
        encoding="utf-8",
    )
    return config


def test_settings_from_missing_config_errors(tmp_path):
    result = runner.invoke(app, ["doctor", "--config", str(tmp_path / "missing.toml")])
    assert result.exit_code != 0
    assert "Config file not found" in result.output


def test_doctor_creates_directories(tmp_path):
    config = _write_config(tmp_path)
    result = runner.invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 0
    assert (tmp_path / "vault").is_dir()
    assert (tmp_path / "inbox").is_dir()
    assert "Vault:" in result.output


def test_mode_command_sets_mode(tmp_path):
    config = _write_config(tmp_path)
    result = runner.invoke(app, ["mode", "silent", "--config", str(config)])
    assert result.exit_code == 0
    assert "silent" in result.output
    assert (tmp_path / "data" / ".mynd-mode").read_text(encoding="utf-8").strip() == "silent"


def test_mode_command_rejects_invalid_mode(tmp_path):
    config = _write_config(tmp_path)
    result = runner.invoke(app, ["mode", "turbo", "--config", str(config)])
    assert result.exit_code != 0


def test_ingest_reports_results_and_closes_client(tmp_path):
    config = _write_config(tmp_path)
    settings = _settings(tmp_path)
    fake_client = MagicMock()
    with patch("mynd.cli.load_settings", return_value=settings), patch(
        "mynd.cli.CrossrefClient", return_value=fake_client
    ), patch(
        "mynd.cli.ingest_inbox",
        return_value=[{"status": "ingested", "paper_id": "paper-1"}],
    ):
        result = runner.invoke(app, ["ingest", "--config", str(config)])
    assert result.exit_code == 0
    assert json.loads(result.output.strip().splitlines()[-1])["status"] == "ingested"
    fake_client.close.assert_called_once()


def test_ingest_without_pdfs_reports_empty(tmp_path):
    config = _write_config(tmp_path)
    settings = _settings(tmp_path)
    with patch("mynd.cli.load_settings", return_value=settings), patch(
        "mynd.cli.CrossrefClient", return_value=MagicMock()
    ), patch("mynd.cli.ingest_inbox", return_value=[]):
        result = runner.invoke(app, ["ingest", "--config", str(config)])
    assert result.exit_code == 0
    assert "No PDFs found" in result.output


def test_review_queue_command_prints_path(tmp_path):
    config = _write_config(tmp_path)
    settings = _settings(tmp_path)
    expected = tmp_path / "vault" / "review.md"
    with patch("mynd.cli.load_settings", return_value=settings), patch(
        "mynd.cli.write_review_queue", return_value=expected
    ):
        result = runner.invoke(app, ["review-queue", "--config", str(config)])
    assert result.exit_code == 0
    assert str(expected) in result.output


def test_settings_from_reports_malformed_config(tmp_path):
    config = tmp_path / "bad.toml"
    config.write_text("this is not toml", encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--config", str(config)])

    assert result.exit_code != 0
    assert "Invalid config" in result.output


def test_settings_from_reports_missing_required_keys(tmp_path):
    config = tmp_path / "partial.toml"
    config.write_text('vault_path = "vault"\n', encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--config", str(config)])

    assert result.exit_code != 0
    assert "database_path" in result.output
