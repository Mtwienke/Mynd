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
    allow_remote_services: bool = False
    slides_inbox_path: Path | None = None
    topics_folder: str = "20 Topics"
    maintenance_excludes: tuple[str, ...] = ()
    topic_web_enabled: bool = False
    topic_web_cache_days: int = 30
    topic_web_provider: str = "ddgs"

    def __post_init__(self) -> None:
        folder = Path(self.topics_folder)
        if folder.is_absolute() or ".." in folder.parts or not folder.parts:
            raise ValueError("topics_folder must be a nonempty vault-relative folder")
        if self.topic_web_cache_days < 1:
            raise ValueError("topic_web_cache_days must be positive")
        if self.topic_web_provider not in ("ddgs", "ollama"):
            raise ValueError("topic_web_provider must be 'ddgs' or 'ollama'")

    @property
    def slides_inbox(self) -> Path:
        """A separate inbox prevents course PDFs from entering the paper pipeline."""
        return self.slides_inbox_path or self.inbox_path / "Slides"

    @property
    def slides_library(self) -> Path:
        return self.vault_path / "15 Slides"

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
    excludes = raw.get("maintenance_excludes", [])
    if not isinstance(excludes, list) or not all(isinstance(item, str) for item in excludes):
        raise ValueError("maintenance_excludes must be a list of glob patterns")
    web_enabled = raw.get("topic_web_enabled", False)
    if not isinstance(web_enabled, bool):
        raise TypeError("topic_web_enabled must be true or false")

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
        allow_remote_services=bool(raw.get("allow_remote_services", False)),
        slides_inbox_path=resolve(raw["slides_inbox_path"]) if "slides_inbox_path" in raw else None,
        topics_folder=str(raw.get("topics_folder", "20 Topics")),
        maintenance_excludes=tuple(excludes),
        topic_web_enabled=web_enabled,
        topic_web_cache_days=int(raw.get("topic_web_cache_days", 30)),
        topic_web_provider=raw.get("topic_web_provider", "ddgs"),
    )
