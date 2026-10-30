from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Resolved runtime paths and feature switches loaded from config.toml."""

    vault_path: Path
    data_path: Path
    inbox_path: Path
    pdf_store_path: Path
    database_path: Path
    grobid_url: str = "http://localhost:8070"
    use_grobid: bool = True
    crossref_email: str = "you@example.com"
    use_local_llm: bool = True


def load_settings(path: Path) -> Settings:
    """Read config.toml and resolve relative paths against its directory."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent

    def resolve(value: str) -> Path:
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else (base / candidate).resolve()

    return Settings(
        vault_path=resolve(raw["vault_path"]),
        data_path=resolve(raw["data_path"]),
        inbox_path=resolve(raw["inbox_path"]),
        pdf_store_path=resolve(raw["pdf_store_path"]),
        database_path=resolve(raw["database_path"]),
        grobid_url=raw.get("grobid_url", "http://localhost:8070").rstrip("/"),
        use_grobid=bool(raw.get("use_grobid", True)),
        crossref_email=raw.get("crossref_email", "you@example.com"),
        use_local_llm=raw.get("use_local_llm", True),
    )
