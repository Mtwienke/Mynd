"""Vault-scoped, versioned maintenance state; dry runs never initialize a database."""
from __future__ import annotations

import json
import sqlite3

from mynd.settings import Settings
from mynd.storage.database import connect, initialize_database


class MaintenanceStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.vault = str(settings.vault_path.resolve())
        self.original: dict[tuple[str, str], str] = {}
        path = settings.database_path
        if path.exists():
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='vault_maintenance'"
                ).fetchone()
                if exists:
                    for namespace, key, payload in db.execute(
                        "SELECT namespace, key, payload_json FROM vault_maintenance WHERE vault=?",
                        (self.vault,),
                    ):
                        self.original[namespace, key] = payload
        self.records = {key: json.loads(value) for key, value in self.original.items()}

    def get(self, namespace: str, key: str, default=None):
        return self.records.get((namespace, key), default)

    def items(self, namespace: str) -> dict:
        return {key: value for (ns, key), value in self.records.items() if ns == namespace}

    def put(self, namespace: str, key: str, value) -> None:
        self.records[namespace, key] = value

    def delete(self, namespace: str, key: str) -> None:
        self.records.pop((namespace, key), None)

    def save(self) -> None:
        encoded = {
            key: json.dumps(value, ensure_ascii=False, sort_keys=True)
            for key, value in self.records.items()
        }
        changed = {key: value for key, value in encoded.items()
                   if self.original.get(key) != value}
        removed = self.original.keys() - encoded.keys()
        if not changed and not removed:
            return
        initialize_database(self.settings.database_path)
        with connect(self.settings.database_path) as db:
            db.executemany(
                "INSERT INTO vault_maintenance VALUES (?, ?, ?, ?) "
                "ON CONFLICT(vault, namespace, key) DO UPDATE SET payload_json=excluded.payload_json",
                [(self.vault, ns, key, value) for (ns, key), value in changed.items()],
            )
            db.executemany(
                "DELETE FROM vault_maintenance WHERE vault=? AND namespace=? AND key=?",
                [(self.vault, ns, key) for ns, key in removed],
            )
        self.original = encoded
