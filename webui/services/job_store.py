"""SQLite-backed job state storage for WebUI background tasks."""

from __future__ import annotations

import datetime
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any


class JobStore:
    """Persist background job status in SQLite.

    Flask currently runs tasks in background threads, while a future Robyn
    migration may run more than one worker. Keeping job state in SQLite avoids
    depending on process-local dictionaries for user-visible task progress.
    """

    def __init__(self, db_path: Path, max_logs: int = 120):
        self.db_path = Path(db_path)
        self.max_logs = max_logs
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path, timeout=30)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS webui_jobs (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    params_json TEXT NOT NULL,
                    logs_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    progress_json TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            try:
                conn.execute("ALTER TABLE webui_jobs ADD COLUMN progress_json TEXT")
            except sqlite3.OperationalError:
                pass  # 列已存在(新建表或已迁移)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_webui_jobs_created_at "
                "ON webui_jobs(created_at DESC)"
            )

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _json_loads(value: str | None, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now().isoformat()

    def create(self, job: dict[str, Any]) -> dict[str, Any]:
        row = dict(job)
        row.setdefault("created_at", self._now())
        row.setdefault("status", "queued")
        row.setdefault("started_at", None)
        row.setdefault("finished_at", None)
        row.setdefault("params", {})
        row.setdefault("logs", [])
        row.setdefault("result", None)
        row.setdefault("error", None)
        row.setdefault("progress", None)

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO webui_jobs (
                    id, type, status, created_at, started_at, finished_at,
                    params_json, logs_json, result_json, error, progress_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["type"],
                    row["status"],
                    row["created_at"],
                    row["started_at"],
                    row["finished_at"],
                    self._json_dumps(row["params"]),
                    self._json_dumps(row["logs"]),
                    self._json_dumps(row["result"]) if row["result"] is not None else None,
                    row["error"],
                    self._json_dumps(row["progress"]) if row["progress"] is not None else None,
                    self._now(),
                ),
            )
        return row

    def update(self, job_id: str, **updates: Any) -> dict[str, Any] | None:
        allowed_fields = {"status", "started_at", "finished_at", "params", "logs", "result", "error", "progress"}
        updates = {key: value for key, value in updates.items() if key in allowed_fields}
        if not updates:
            return self.get(job_id)

        columns: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key == "params":
                columns.append("params_json = ?")
                values.append(self._json_dumps(value))
            elif key == "logs":
                columns.append("logs_json = ?")
                values.append(self._json_dumps(value))
            elif key == "result":
                columns.append("result_json = ?")
                values.append(self._json_dumps(value) if value is not None else None)
            elif key == "progress":
                columns.append("progress_json = ?")
                values.append(self._json_dumps(value) if value is not None else None)
            else:
                columns.append(f"{key} = ?")
                values.append(value)

        columns.append("updated_at = ?")
        values.append(self._now())
        values.append(job_id)

        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE webui_jobs SET {', '.join(columns)} WHERE id = ?",
                values,
            )
            if cursor.rowcount == 0:
                return None
        return self.get(job_id)

    def append_log(self, job_id: str, message: str) -> dict[str, Any] | None:
        with self._lock:
            job = self.get(job_id)
            if not job:
                return None

            logs = list(job.get("logs") or [])
            logs.append(message)
            if len(logs) > self.max_logs:
                logs = logs[-self.max_logs:]
            return self.update(job_id, logs=logs)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM webui_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM webui_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_by_status(self, statuses: list[str] | tuple[str, ...], limit: int = 100) -> list[dict[str, Any]]:
        clean_statuses = [str(status) for status in statuses if status]
        if not clean_statuses:
            return []
        placeholders = ",".join("?" for _ in clean_statuses)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM webui_jobs WHERE status IN ({placeholders}) "
                "ORDER BY created_at DESC LIMIT ?",
                [*clean_statuses, limit],
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def _row_to_job(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": row["type"],
            "status": row["status"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "params": self._json_loads(row["params_json"], {}),
            "logs": self._json_loads(row["logs_json"], []),
            "result": self._json_loads(row["result_json"], None),
            "error": row["error"],
            "progress": self._json_loads(
                row["progress_json"] if "progress_json" in row.keys() else None, None
            ),
        }
