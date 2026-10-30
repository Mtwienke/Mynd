from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_GROBID_URL = "http://localhost:8070"
DEFAULT_CROSSREF_EMAIL = "you@example.com"
_REQUIRED_KEYS = (
    "vault_path",
    "data_path",
    "inbox_path",
    "pdf_store_path",
    "database_path",
)


@dataclass(frozen=True)
class Settings:
    """Resolved runtime paths and feature switches loaded from config.toml."""

    vault_path: Path
    data_path: Path
    inbox_path: Path
    pdf_store_path: Path
    database_path: Path
    grobid_url: str = DEFAULT_GROBID_URL
    use_grobid: bool = True
    crossref_email: str = DEFAULT_CROSSREF_EMAIL
    use_local_llm: bool = True
    allow_remote_services: bool = False

    @property
    def mode_file(self) -> Path:
        """Path of the shared inference-mode file used by CLI and ingestion."""
        return self.data_path / ".mynd-mode"


def load_settings(path: Path) -> Settings:
    """Read config.toml and resolve relative paths against its directory."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in _REQUIRED_KEYS if key not in raw]
    if missing:
        raise ValueError(
            f"{path} is missing required setting(s): {', '.join(missing)}"
        )
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
        grobid_url=str(raw.get("grobid_url", DEFAULT_GROBID_URL)).rstrip("/"),
        use_grobid=bool(raw.get("use_grobid", True)),
        crossref_email=raw.get("crossref_email", DEFAULT_CROSSREF_EMAIL),
        use_local_llm=bool(raw.get("use_local_llm", True)),
        allow_remote_services=bool(raw.get("allow_remote_services", False)),
    )
