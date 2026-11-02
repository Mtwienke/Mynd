from __future__ import annotations

import hashlib
import os
from pathlib import Path

_HASH_CHUNK_SIZE = 1 << 20


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, content: str) -> None:
    """Write text via a temporary sibling and atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)
