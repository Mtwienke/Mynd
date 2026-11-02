import pytest


@pytest.fixture
def config_file(tmp_path):
    """Write a valid config.toml with paths rooted at tmp_path."""
    config = tmp_path / "config.toml"
    config.write_text(
        f'vault_path = "{(tmp_path / "vault").as_posix()}"\n'
        f'data_path = "{(tmp_path / "data").as_posix()}"\n'
        f'inbox_path = "{(tmp_path / "inbox").as_posix()}"\n'
        f'pdf_store_path = "{(tmp_path / "pdfs").as_posix()}"\n'
        f'database_path = "{(tmp_path / "library.sqlite3").as_posix()}"\n'
        "use_grobid = false\n",
        encoding="utf-8",
    )
    return config
