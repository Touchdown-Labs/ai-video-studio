from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping


class SQLiteProjectStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            if self.database_path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    data_json TEXT NOT NULL
                )
                """
            )
            self._connection.commit()

    @property
    def persistent(self) -> bool:
        return self.database_path != ":memory:"

    def create(self, project_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        encoded = self._encode(data)
        with self._lock:
            self._connection.execute(
                "INSERT INTO projects (project_id, data_json) VALUES (?, ?)",
                (project_id, encoded),
            )
            self._connection.commit()
        project = self.get(project_id)
        if project is None:
            raise RuntimeError("project was not persisted")
        return project

    def update(self, project_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT data_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(project_id)
            data = self._decode(row["data_json"])
            data.update(patch)
            self._connection.execute(
                """
                UPDATE projects
                SET data_json = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE project_id = ?
                """,
                (self._encode(data), project_id),
            )
            self._connection.commit()
        project = self.get(project_id)
        if project is None:
            raise RuntimeError("project disappeared after update")
        return project

    def get(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT project_id, created_at, updated_at, data_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT project_id, created_at, updated_at, data_json
                FROM projects
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _encode(value: Mapping[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

    @staticmethod
    def _decode(value: str) -> dict[str, Any]:
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError("stored project must be a JSON object")
        return decoded

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = self._decode(row["data_json"])
        return {
            "project_id": row["project_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            **data,
        }
