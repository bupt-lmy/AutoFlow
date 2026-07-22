"""Small SQLite repository for platform definitions and run observability."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from axonflow.media.models import (
    AssetKind,
    AssetStatus,
    MediaAsset,
    RenderJob,
    RenderJobStatus,
)
from axonflow.platform.credentials import CredentialCipher, masked_secret
from axonflow.platform.models import PlatformWorkflow


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PlatformStore:
    """Persistence that works locally today and has clear PostgreSQL seams later."""

    def __init__(self, database_path: Path | str) -> None:
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._cipher = CredentialCipher(path.parent)
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_definitions (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_data TEXT NOT NULL,
                    workflow_snapshot TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    result TEXT
                );
                CREATE TABLE IF NOT EXISTS node_runs (
                    run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    output TEXT,
                    error TEXT,
                    PRIMARY KEY (run_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS run_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    node_id TEXT,
                    name TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    media_type TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS media_assets (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_media_assets_kind
                  ON media_assets(kind, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_media_assets_status
                  ON media_assets(status, created_at DESC);
                CREATE TABLE IF NOT EXISTS render_jobs (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_render_jobs_status
                  ON render_jobs(status, created_at DESC);
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    source TEXT NOT NULL,
                    env_var TEXT,
                    encrypted_secret TEXT,
                    masked_value TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observability_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_spans (
                    id TEXT PRIMARY KEY,
                    run_id TEXT,
                    workflow_id TEXT,
                    execution_id TEXT,
                    agent_id TEXT,
                    trace_kind TEXT NOT NULL DEFAULT 'unscoped',
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    credential_id TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    latency_ms INTEGER,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    input_preview TEXT,
                    output_preview TEXT,
                    error TEXT,
                    langsmith_trace_url TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_llm_spans_run ON llm_spans(run_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_llm_spans_workflow
                  ON llm_spans(workflow_id, started_at DESC);
                """
            )
            span_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(llm_spans)").fetchall()
            }
            if "trace_kind" not in span_columns:
                self._connection.execute(
                    "ALTER TABLE llm_spans "
                    "ADD COLUMN trace_kind TEXT NOT NULL DEFAULT 'unscoped'"
                )
            # Before trace_kind existed, business Agent spans already carried agent_id.
            self._connection.execute(
                "UPDATE llm_spans SET trace_kind = 'agent' "
                "WHERE trace_kind = 'unscoped' AND agent_id IS NOT NULL AND agent_id != ''"
            )
            # Older retries could leave the failed attempt's error on a recovered node.
            self._connection.execute(
                "UPDATE node_runs SET error = NULL "
                "WHERE status = 'completed' AND error IS NOT NULL"
            )
            self._connection.commit()

    def save_media_asset(self, asset: MediaAsset) -> MediaAsset:
        """Create or replace media metadata without touching the referenced bytes."""
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO media_assets(id, payload, kind, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  payload = excluded.payload,
                  kind = excluded.kind,
                  status = excluded.status,
                  updated_at = excluded.updated_at
                """,
                (
                    asset.id,
                    asset.model_dump_json(),
                    asset.kind.value,
                    asset.status.value,
                    asset.created_at,
                    asset.updated_at,
                ),
            )
            self._connection.commit()
        return asset

    def get_media_asset(self, asset_id: str) -> MediaAsset | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM media_assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
        return MediaAsset.model_validate_json(row["payload"]) if row else None

    def list_media_assets(
        self,
        kind: AssetKind | None = None,
        status: AssetStatus | None = None,
    ) -> list[MediaAsset]:
        clauses: list[str] = []
        values: list[str] = []
        if kind is not None:
            clauses.append("kind = ?")
            values.append(kind.value)
        if status is not None:
            clauses.append("status = ?")
            values.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT payload FROM media_assets {where} ORDER BY created_at DESC",
                values,
            ).fetchall()
        return [MediaAsset.model_validate_json(row["payload"]) for row in rows]

    def delete_media_asset(self, asset_id: str) -> bool:
        """Delete only the registry entry; object deletion is a separate explicit operation."""
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM media_assets WHERE id = ?",
                (asset_id,),
            )
            self._connection.commit()
        return cursor.rowcount > 0

    def save_render_job(self, job: RenderJob) -> RenderJob:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO render_jobs(id, payload, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  payload = excluded.payload,
                  status = excluded.status,
                  updated_at = excluded.updated_at
                """,
                (
                    job.id,
                    job.model_dump_json(),
                    job.status.value,
                    job.created_at,
                    job.updated_at,
                ),
            )
            self._connection.commit()
        return job

    def get_render_job(self, job_id: str) -> RenderJob | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM render_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return RenderJob.model_validate_json(row["payload"]) if row else None

    def list_render_jobs(self, status: RenderJobStatus | None = None) -> list[RenderJob]:
        if status is None:
            query = "SELECT payload FROM render_jobs ORDER BY created_at DESC"
            values: tuple[str, ...] = ()
        else:
            query = "SELECT payload FROM render_jobs WHERE status = ? ORDER BY created_at DESC"
            values = (status.value,)
        with self._lock:
            rows = self._connection.execute(query, values).fetchall()
        return [RenderJob.model_validate_json(row["payload"]) for row in rows]

    def get_workflow(self, workflow_id: str) -> PlatformWorkflow | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM workflow_definitions WHERE id = ?", (workflow_id,)
            ).fetchone()
        return PlatformWorkflow.model_validate_json(row["payload"]) if row else None

    def list_workflows(self) -> list[PlatformWorkflow]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM workflow_definitions ORDER BY updated_at DESC"
            ).fetchall()
        return [PlatformWorkflow.model_validate_json(row["payload"]) for row in rows]

    def save_workflow(self, workflow: PlatformWorkflow) -> PlatformWorkflow:
        payload = workflow.model_dump_json()
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO workflow_definitions(id, payload, revision, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(id) DO UPDATE SET
                  payload = excluded.payload,
                  revision = workflow_definitions.revision + 1,
                  updated_at = excluded.updated_at
                """,
                (workflow.id, payload, now),
            )
            self._connection.commit()
        return workflow

    def create_run(self, run_id: str, workflow: PlatformWorkflow, input_data: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO workflow_runs(
                  run_id, workflow_id, status, input_data, workflow_snapshot, started_at
                ) VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (run_id, workflow.id, input_data, workflow.model_dump_json(), _now()),
            )
            self._connection.commit()

    def complete_run(self, run_id: str, status: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE workflow_runs "
                "SET status = ?, completed_at = ?, result = ? "
                "WHERE run_id = ?",
                (status, _now(), json.dumps(result, ensure_ascii=False), run_id),
            )
            self._connection.commit()

    def create_credential(
        self,
        name: str,
        provider: str,
        source: str,
        secret: str | None = None,
        env_var: str | None = None,
    ) -> dict[str, Any]:
        if source not in {"encrypted", "environment"}:
            raise ValueError("Credential source must be encrypted or environment")
        if source == "encrypted" and not secret:
            raise ValueError("Encrypted credentials require a secret")
        if source == "environment" and not env_var:
            raise ValueError("Environment credentials require an environment variable")

        credential_id = f"cred-{uuid.uuid4().hex[:12]}"
        now = _now()
        encrypted_secret = self._cipher.encrypt(secret) if secret else None
        masked_value = masked_secret(secret) if secret else f"env:{env_var}"
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO credentials(
                  id, name, provider, source, env_var, encrypted_secret, masked_value,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    name.strip(),
                    provider.strip(),
                    source,
                    env_var.strip() if env_var else None,
                    encrypted_secret,
                    masked_value,
                    now,
                    now,
                ),
            )
            self._connection.commit()
        return self.get_credential(credential_id) or {}

    def list_credentials(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, name, provider, source, env_var, masked_value, created_at, updated_at
                FROM credentials ORDER BY name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_credential(self, credential_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, name, provider, source, env_var, masked_value, created_at, updated_at
                FROM credentials WHERE id = ?
                """,
                (credential_id,),
            ).fetchone()
        return dict(row) if row else None

    def resolve_credential(self, credential_id: str) -> dict[str, str]:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, provider, source, env_var, encrypted_secret "
                "FROM credentials WHERE id = ?",
                (credential_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Credential not found: {credential_id}")
        if row["source"] == "environment":
            import os

            secret = os.environ.get(row["env_var"] or "")
            if not secret:
                raise ValueError(f"Environment variable {row['env_var']} is not set")
        else:
            secret = self._cipher.decrypt(row["encrypted_secret"] or "")
        return {"id": row["id"], "provider": row["provider"], "secret": secret}

    def delete_credential(self, credential_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM credentials WHERE id = ?",
                (credential_id,),
            )
            self._connection.commit()
        return cursor.rowcount > 0

    def update_credential(
        self,
        credential_id: str,
        name: str,
        provider: str,
        source: str,
        secret: str | None = None,
        env_var: str | None = None,
    ) -> dict[str, Any] | None:
        """Update credential metadata and rotate its secret only when one is supplied."""
        if source not in {"encrypted", "environment"}:
            raise ValueError("Credential source must be encrypted or environment")
        with self._lock:
            current = self._connection.execute(
                "SELECT source, encrypted_secret, masked_value FROM credentials WHERE id = ?",
                (credential_id,),
            ).fetchone()
            if current is None:
                return None

            if source == "environment":
                if not env_var:
                    raise ValueError("Environment credentials require an environment variable")
                encrypted_secret = None
                normalized_env_var = env_var.strip()
                masked_value = f"env:{normalized_env_var}"
            else:
                normalized_env_var = None
                if secret:
                    encrypted_secret = self._cipher.encrypt(secret)
                    masked_value = masked_secret(secret)
                elif current["source"] == "encrypted" and current["encrypted_secret"]:
                    encrypted_secret = current["encrypted_secret"]
                    masked_value = current["masked_value"]
                else:
                    raise ValueError("A secret is required when switching to encrypted storage")

            self._connection.execute(
                """
                UPDATE credentials SET
                  name = ?, provider = ?, source = ?, env_var = ?, encrypted_secret = ?,
                  masked_value = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name.strip(),
                    provider.strip(),
                    source,
                    normalized_env_var,
                    encrypted_secret,
                    masked_value,
                    _now(),
                    credential_id,
                ),
            )
            self._connection.commit()
        return self.get_credential(credential_id)

    def create_model_profile(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        profile_id = f"model-{uuid.uuid4().hex[:12]}"
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO model_profiles(id, name, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (profile_id, name.strip(), json.dumps(config, ensure_ascii=False), now, now),
            )
            self._connection.commit()
        return self.get_model_profile(profile_id) or {}

    def list_model_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, name, payload, created_at, updated_at FROM model_profiles ORDER BY name"
            ).fetchall()
        return [self._model_profile_row(row) for row in rows]

    def get_model_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, name, payload, created_at, updated_at FROM model_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
        return self._model_profile_row(row) if row else None

    def delete_model_profile(self, profile_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM model_profiles WHERE id = ?",
                (profile_id,),
            )
            self._connection.commit()
        return cursor.rowcount > 0

    def update_model_profile(
        self,
        profile_id: str,
        name: str,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE model_profiles SET name = ?, payload = ?, updated_at = ? WHERE id = ?
                """,
                (name.strip(), json.dumps(config, ensure_ascii=False), _now(), profile_id),
            )
            self._connection.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_model_profile(profile_id)

    @staticmethod
    def _model_profile_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "config": json.loads(row["payload"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_observability_settings(self) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM observability_settings WHERE id = 1"
            ).fetchone()
        return json.loads(row["payload"]) if row else {
            "langsmith_enabled": False,
            "langsmith_project": "axonflow",
            "langsmith_endpoint": None,
            "langsmith_credential_id": None,
            "content_policy": "masked_content",
        }

    def save_observability_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(settings, ensure_ascii=False)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO observability_settings(id, payload, updated_at) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  payload = excluded.payload,
                  updated_at = excluded.updated_at
                """,
                (payload, _now()),
            )
            self._connection.commit()
        return settings

    def create_llm_span(self, span: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO llm_spans(
                  id, run_id, workflow_id, execution_id, agent_id, trace_kind, provider, model,
                  credential_id,
                  status, started_at, input_preview, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    span["id"],
                    span.get("run_id"),
                    span.get("workflow_id"),
                    span.get("execution_id"),
                    span.get("agent_id"),
                    span.get("trace_kind", "unscoped"),
                    span["provider"],
                    span["model"],
                    span.get("credential_id"),
                    span["started_at"], span.get("input_preview"),
                    json.dumps(span.get("metadata", {}), ensure_ascii=False),
                ),
            )
            self._connection.commit()

    def complete_llm_span(self, span_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE llm_spans SET status = ?, completed_at = ?, latency_ms = ?, input_tokens = ?,
                  output_tokens = ?, total_tokens = ?, output_preview = ?, error = ?,
                  langsmith_trace_url = ?
                WHERE id = ?
                """,
                (
                    result["status"],
                    result.get("completed_at"),
                    result.get("latency_ms"),
                    result.get("input_tokens", 0),
                    result.get("output_tokens", 0),
                    result.get("total_tokens", 0),
                    result.get("output_preview"),
                    result.get("error"),
                    result.get("langsmith_trace_url"),
                    span_id,
                ),
            )
            self._connection.commit()

    def list_llm_spans(
        self,
        run_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        trace_kind: str | None = None,
        exclude_trace_kind: str | None = None,
        attributed_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[str] = []
        filters = (
            ("run_id", run_id),
            ("workflow_id", workflow_id),
            ("agent_id", agent_id),
            ("trace_kind", trace_kind),
        )
        for field, value in filters:
            if value:
                clauses.append(f"{field} = ?")
                values.append(value)
        if exclude_trace_kind:
            clauses.append("trace_kind != ?")
            values.append(exclude_trace_kind)
        if attributed_only:
            clauses.append("agent_id IS NOT NULL AND agent_id != ''")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM llm_spans {where} ORDER BY started_at DESC LIMIT 500", values
            ).fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]

    def list_execution_contexts(self) -> dict[str, tuple[str, str]]:
        """Map persisted engine execution IDs to product run and workflow IDs."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id, workflow_id, result FROM workflow_runs WHERE result IS NOT NULL"
            ).fetchall()
        contexts: dict[str, tuple[str, str]] = {}
        for row in rows:
            try:
                result = json.loads(row["result"])
            except (TypeError, json.JSONDecodeError):
                continue
            execution_id = result.get("workflow_id") if isinstance(result, dict) else None
            if isinstance(execution_id, str) and execution_id:
                contexts[execution_id] = (row["run_id"], row["workflow_id"])
        return contexts

    def update_node_run(
        self,
        run_id: str,
        node_id: str,
        agent_id: str,
        status: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = _now()
        terminal = status in {"completed", "error"}
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO node_runs(
                  run_id, node_id, agent_id, status, started_at, completed_at, output, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, node_id) DO UPDATE SET
                  status = excluded.status,
                  started_at = COALESCE(node_runs.started_at, excluded.started_at),
                  completed_at = excluded.completed_at,
                  output = COALESCE(excluded.output, node_runs.output),
                  error = CASE
                    WHEN excluded.status = 'completed' THEN NULL
                    ELSE COALESCE(excluded.error, node_runs.error)
                  END
                """,
                (
                    run_id,
                    node_id,
                    agent_id,
                    status,
                    now,
                    now if terminal else None,
                    json.dumps(output, ensure_ascii=False) if output is not None else None,
                    error,
                ),
            )
            self._connection.commit()

    def record_event(
        self,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
        timestamp: str,
    ) -> dict:
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO run_events(run_id, event_type, timestamp, data) VALUES (?, ?, ?, ?)",
                (run_id, event_type, timestamp, json.dumps(data, ensure_ascii=False)),
            )
            self._connection.commit()
        return {
            "sequence": cursor.lastrowid,
            "type": event_type,
            "run_id": run_id,
            "timestamp": timestamp,
            "data": data,
        }

    def list_runs(self, workflow_id: str) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ? ORDER BY started_at DESC",
                (workflow_id,),
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def get_run(self, workflow_id: str, run_id: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ? AND run_id = ?",
                (workflow_id, run_id),
            ).fetchone()
            node_rows = self._connection.execute(
                "SELECT * FROM node_runs WHERE run_id = ? ORDER BY started_at", (run_id,)
            ).fetchall()
            event_rows = self._connection.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
        if row is None:
            return None
        result = self._run_row(row)
        result["workflow_snapshot"] = json.loads(row["workflow_snapshot"])
        result["node_runs"] = [
            {
                **dict(node_row),
                "output": json.loads(node_row["output"]) if node_row["output"] else None,
            }
            for node_row in node_rows
        ]
        result["events"] = [
            {
                "sequence": event_row["sequence"],
                "type": event_row["event_type"],
                "run_id": run_id,
                "timestamp": event_row["timestamp"],
                "data": json.loads(event_row["data"]),
            }
            for event_row in event_rows
        ]
        return result

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        return {
            "run_id": row["run_id"],
            "workflow_id": row["workflow_id"],
            "status": row["status"],
            "input": row["input_data"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "result": json.loads(row["result"]) if row["result"] else None,
        }
