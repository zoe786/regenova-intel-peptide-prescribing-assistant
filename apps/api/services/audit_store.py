"""Audit persistence layer using SQLite.

Provides thread-safe logging of:
- Chat query events (query hash, role, confidence, safety flags)
- File upload events (filename, size, result)
- URL ingestion events (url, source_type, chunk count)
- Ingest job records (per-run per-source breakdown)
- Admin actions (chunk/source edits and deletions)

All tables are created on first access via _ensure_schema().
WAL mode is enabled for concurrent read safety.
IP addresses are hashed using HMAC-SHA256 with a per-deployment secret salt
to provide privacy while preventing rainbow-table attacks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CREATE_AUDIT_EVENTS = """
CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL,
    request_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL DEFAULT '',
    data        TEXT    NOT NULL DEFAULT '{}',
    ip_hash     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_events_type      ON audit_events(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_events_timestamp ON audit_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_events_request_id ON audit_events(request_id);
"""

_CREATE_INGEST_JOBS = """
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT    NOT NULL UNIQUE,
    source_type   TEXT    NOT NULL DEFAULT 'all',
    triggered_at  TEXT    NOT NULL,
    completed_at  TEXT,
    status        TEXT    NOT NULL DEFAULT 'queued',
    total_chunks  INTEGER NOT NULL DEFAULT 0,
    results       TEXT    NOT NULL DEFAULT '{}',
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_jobs_job_id     ON ingest_jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_ingest_jobs_status     ON ingest_jobs(status);
CREATE INDEX IF NOT EXISTS idx_ingest_jobs_triggered  ON ingest_jobs(triggered_at);
"""


class AuditStore:
    """Thread-safe SQLite-backed audit store.

    A single instance is shared across the FastAPI app via app.state.
    All write operations acquire a threading.Lock to prevent concurrent
    SQLite writes from multiple worker threads.
    """

    def __init__(self, db_path: str = "./data/audit.db", ip_salt: str = "") -> None:
        """Initialise the audit store and ensure the schema exists.

        Args:
            db_path: Path to the SQLite database file.
            ip_salt: Secret salt used when hashing IP addresses (HMAC-SHA256).
                     Defaults to the AUDIT_IP_SALT env-var or a random value.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Use caller-supplied salt, env-var, or generate a one-time random salt.
        self._ip_salt: bytes = (
            ip_salt or os.getenv("AUDIT_IP_SALT", "")
        ).encode() or os.urandom(32)
        self._lock = threading.Lock()
        self._ensure_schema()
        logger.info("AuditStore initialised at %s", self.db_path)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """Open a new SQLite connection in WAL mode."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        """Create tables and indexes if they do not already exist."""
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_CREATE_AUDIT_EVENTS)
                conn.executescript(_CREATE_INGEST_JOBS)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    def _hash_ip(self, ip: str) -> str:
        """HMAC-SHA256 hash of an IP address for privacy compliance.

        Using a salted HMAC prevents rainbow-table attacks against
        the small IPv4 address space while still enabling correlation
        of events from the same source within a deployment.
        """
        if not ip:
            return ""
        return hmac.new(self._ip_salt, ip.encode(), hashlib.sha256).hexdigest()[:24]

    # ── Audit Events ──────────────────────────────────────────────────────────

    def log_event(
        self,
        event_type: str,
        data: dict[str, Any],
        role: str = "",
        request_id: str | None = None,
        ip: str = "",
    ) -> str:
        """Insert one audit event and return its request_id.

        Args:
            event_type: One of chat_query / upload / ingest_trigger / admin_action.
            data: Arbitrary JSON-serialisable dict with event-specific payload.
            role: User role string (clinician / admin / researcher).
            request_id: Optional caller-supplied ID; generated if omitted.
            ip: Client IP address (will be one-way hashed before storage).

        Returns:
            The request_id used for this event.
        """
        req_id = request_id or str(uuid.uuid4())
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO audit_events "
                    "(event_type, timestamp, request_id, role, data, ip_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event_type,
                        self._now(),
                        req_id,
                        role,
                        json.dumps(data, default=str),
                        self._hash_ip(ip),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return req_id

    def list_events(
        self,
        event_type: str | None = None,
        role: str | None = None,
        since: str | None = None,
        until: str | None = None,
        request_id_prefix: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Query audit events with optional filters.

        Args:
            event_type: Filter by exact event type.
            role: Filter by role.
            since: ISO-8601 timestamp lower bound (inclusive).
            until: ISO-8601 timestamp upper bound (inclusive).
            request_id_prefix: Filter by request_id prefix (LIKE).
            limit: Maximum rows to return.
            offset: Pagination offset.

        Returns:
            List of event dicts ordered by timestamp DESC.
        """
        conditions: list[str] = []
        params: list[Any] = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if role:
            conditions.append("role = ?")
            params.append(role)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("timestamp <= ?")
            params.append(until)
        if request_id_prefix:
            conditions.append("request_id LIKE ?")
            params.append(f"{request_id_prefix}%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM audit_events {where} "
                "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def count_events(
        self,
        event_type: str | None = None,
        role: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int:
        """Return total count matching optional filters (for pagination)."""
        conditions: list[str] = []
        params: list[Any] = []
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if role:
            conditions.append("role = ?")
            params.append(role)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("timestamp <= ?")
            params.append(until)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT COUNT(*) FROM audit_events {where}", params
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()

    # ── Ingest Jobs ───────────────────────────────────────────────────────────

    def log_ingest_job(
        self,
        source_type: str = "all",
        job_id: str | None = None,
    ) -> str:
        """Create a new ingest job record in 'queued' status.

        Args:
            source_type: Which ingestor was triggered ('all' or specific type).
            job_id: Optional caller-supplied job ID; generated if omitted.

        Returns:
            The job_id string.
        """
        jid = job_id or str(uuid.uuid4())
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO ingest_jobs "
                    "(job_id, source_type, triggered_at, status) VALUES (?, ?, ?, ?)",
                    (jid, source_type, self._now(), "queued"),
                )
                conn.commit()
            finally:
                conn.close()
        return jid

    def update_ingest_job(
        self,
        job_id: str,
        status: str,
        total_chunks: int = 0,
        results: dict | None = None,
        error: str | None = None,
    ) -> None:
        """Update an ingest job's status and results on completion.

        Args:
            job_id: The job to update.
            status: New status string (running / completed / failed).
            total_chunks: Total chunks processed.
            results: Per-source result breakdown dict.
            error: Error message if failed.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE ingest_jobs SET status=?, completed_at=?, "
                    "total_chunks=?, results=?, error=? WHERE job_id=?",
                    (
                        status,
                        self._now() if status in ("completed", "failed") else None,
                        total_chunks,
                        json.dumps(results or {}, default=str),
                        error,
                        job_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def list_ingest_jobs(
        self,
        source_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Query ingest jobs with optional filters.

        Args:
            source_type: Filter by source type.
            status: Filter by status string.
            limit: Maximum rows to return.
            offset: Pagination offset.

        Returns:
            List of job dicts ordered by triggered_at DESC.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if source_type:
            conditions.append("source_type = ?")
            params.append(source_type)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM ingest_jobs {where} "
                "ORDER BY triggered_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            result = []
            for r in rows:
                row_dict = dict(r)
                try:
                    row_dict["results"] = json.loads(row_dict.get("results") or "{}")
                except (json.JSONDecodeError, TypeError):
                    row_dict["results"] = {}
                result.append(row_dict)
            return result
        finally:
            conn.close()

    def get_ingest_job(self, job_id: str) -> dict | None:
        """Return a single ingest job by job_id, or None if not found."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM ingest_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            row_dict = dict(row)
            try:
                row_dict["results"] = json.loads(row_dict.get("results") or "{}")
            except (json.JSONDecodeError, TypeError):
                row_dict["results"] = {}
            return row_dict
        finally:
            conn.close()
