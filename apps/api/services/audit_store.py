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

_CREATE_VIDEO_OUTCOMES = """
CREATE TABLE IF NOT EXISTS ingest_video_outcomes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT    NOT NULL,
    batch_index  INTEGER NOT NULL DEFAULT 0,
    video_id     TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    reason       TEXT    NOT NULL DEFAULT '',
    chunks       INTEGER NOT NULL DEFAULT 0,
    recorded_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_outcomes_job    ON ingest_video_outcomes(job_id);
CREATE INDEX IF NOT EXISTS idx_video_outcomes_status ON ingest_video_outcomes(status);
"""

_CREATE_PDF_QUARANTINE = """
CREATE TABLE IF NOT EXISTS pdf_quarantine (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    quarantined_at    TEXT    NOT NULL,
    job_id            TEXT,
    document_id       TEXT    NOT NULL,
    source_name       TEXT    NOT NULL DEFAULT '',
    file_path         TEXT    NOT NULL DEFAULT '',
    extraction_method TEXT    NOT NULL DEFAULT '',
    warnings          TEXT    NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_pdf_quarantine_quarantined_at ON pdf_quarantine(quarantined_at);
CREATE INDEX IF NOT EXISTS idx_pdf_quarantine_job_id ON pdf_quarantine(job_id);
CREATE INDEX IF NOT EXISTS idx_pdf_quarantine_document_id ON pdf_quarantine(document_id);
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
        provided_salt = (ip_salt or os.getenv("AUDIT_IP_SALT", "")).encode()
        if provided_salt:
            self._ip_salt: bytes = provided_salt
        else:
            self._ip_salt = os.urandom(32)
            logger.warning(
                "AUDIT_IP_SALT not set — using a random per-process salt. "
                "IP hashes will not be consistent across restarts. "
                "Set AUDIT_IP_SALT in production for stable correlation."
            )
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
                conn.executescript(_CREATE_VIDEO_OUTCOMES)
                conn.executescript(_CREATE_PDF_QUARANTINE)
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

    def fail_orphaned_running_jobs(
        self,
        reason: str = (
            "Marked failed on worker startup: orphaned by a previous worker "
            "exit, crash, or restart (no live job was resumed)."
        ),
    ) -> int:
        """Move every ingest job still in 'running' to 'failed'.

        Called on worker startup so a restart self-heals stale rows instead of
        leaving them pinned at 'running' forever (the admin UI reads this table
        and has no way to clear them otherwise).

        Safe for this single-worker deployment: a freshly started worker never
        resumes an in-flight job, so any 'running' row at startup is by
        definition orphaned. If this is ever run with multiple concurrent
        workers, gate it on job age or a heartbeat instead — otherwise one
        worker's boot would fail a healthy run still executing on a peer.

        Returns:
            Number of rows transitioned to 'failed'.
        """
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE ingest_jobs SET status='failed', completed_at=?, "
                    "error=? WHERE status='running'",
                    (self._now(), reason),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def init_ingest_job_batches(
        self, job_id: str, videos_total: int, batches_total: int
    ) -> None:
        """Mark a job 'running' and seed batch-progress in its results JSON."""
        results = {
            "mode": "batched",
            "videos_total": int(videos_total),
            "batches": {"total": int(batches_total), "done": 0},
            "counts": {"ingested": 0, "skipped": 0, "failed": 0},
        }
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE ingest_jobs SET status='running', results=? WHERE job_id=?",
                    (json.dumps(results, default=str), job_id),
                )
                conn.commit()
            finally:
                conn.close()

    def bump_ingest_job_progress(
        self,
        job_id: str,
        add_chunks: int = 0,
        batches_done_increment: int = 0,
        counts: dict | None = None,
        errors: list[str] | None = None,
        report_path: str | None = None,
    ) -> dict:
        """Atomically fold one batch's results into the parent job row.

        Read-modify-write under the store lock, so concurrent batches can't lose
        updates (and it stays correct even though this deployment serialises
        them). Returns {done, total, complete, status}. When the final batch
        lands, status becomes 'completed' (or 'failed' if nothing was ingested).
        """
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT total_chunks, results, error FROM ingest_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    return {"done": 0, "total": 0, "complete": False, "status": "unknown"}
                try:
                    results = json.loads(row["results"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    results = {}
                batches = results.setdefault("batches", {"total": 0, "done": 0})
                batches["done"] = int(batches.get("done", 0)) + int(batches_done_increment)
                c = results.setdefault("counts", {"ingested": 0, "skipped": 0, "failed": 0})
                for k, v in (counts or {}).items():
                    c[k] = int(c.get(k, 0)) + int(v)
                total_chunks = int(row["total_chunks"] or 0) + int(add_chunks)
                if report_path:
                    results["report_path"] = report_path

                existing_err = row["error"] or ""
                if errors:
                    joined = "; ".join(e for e in errors if e)
                    if joined:
                        existing_err = (
                            (existing_err + " | " + joined) if existing_err else joined
                        )[:2000]

                done = int(batches.get("done", 0))
                total = int(batches.get("total", 0))
                complete = total > 0 and done >= total
                status = "running"
                completed_at = None
                if complete:
                    status = "completed" if total_chunks > 0 else "failed"
                    completed_at = self._now()

                conn.execute(
                    "UPDATE ingest_jobs SET status=?, completed_at=?, total_chunks=?, "
                    "results=?, error=? WHERE job_id=?",
                    (
                        status,
                        completed_at,
                        total_chunks,
                        json.dumps(results, default=str),
                        existing_err or None,
                        job_id,
                    ),
                )
                conn.commit()
                return {"done": done, "total": total, "complete": complete, "status": status}
            finally:
                conn.close()

    def log_video_outcomes(
        self, job_id: str, batch_index: int, outcomes: list[dict]
    ) -> int:
        """Persist per-video {video_id, status, reason, chunks} rows for a job."""
        if not outcomes:
            return 0
        now = self._now()
        rows = [
            (
                job_id,
                int(batch_index),
                str(o.get("video_id", "")),
                str(o.get("status", "")),
                str(o.get("reason", "") or "")[:500],
                int(o.get("chunks", 0) or 0),
                now,
            )
            for o in outcomes
        ]
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT INTO ingest_video_outcomes "
                    "(job_id, batch_index, video_id, status, reason, chunks, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
        return len(rows)

    def get_video_outcomes(self, job_id: str) -> list[dict]:
        """Return all per-video outcome rows for a job (batch then insert order)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT batch_index, video_id, status, reason, chunks, recorded_at "
                "FROM ingest_video_outcomes WHERE job_id=? ORDER BY batch_index, id",
                (job_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def summarize_video_outcomes(self, job_id: str) -> dict:
        """Return {ingested, skipped, failed, chunks} totals for a job."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) n, COALESCE(SUM(chunks),0) c "
                "FROM ingest_video_outcomes WHERE job_id=? GROUP BY status",
                (job_id,),
            ).fetchall()
            summary = {"ingested": 0, "skipped": 0, "failed": 0, "chunks": 0}
            for r in rows:
                summary[r["status"]] = r["n"]
                summary["chunks"] += int(r["c"])
            return summary
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

    # ── PDF Quarantine ────────────────────────────────────────────────────────

    def log_pdf_quarantine_records(self, records: list[dict], job_id: str | None = None) -> int:
        """Persist quarantined PDF diagnostics for admin visibility."""
        if not records:
            return 0

        rows: list[tuple[str, str | None, str, str, str, str, str]] = []
        now = self._now()
        for record in records:
            warnings = record.get("warnings") or []
            if isinstance(warnings, str):
                warnings = [warnings]
            rows.append(
                (
                    now,
                    job_id,
                    str(record.get("document_id", "")),
                    str(record.get("source_name", "")),
                    str(record.get("file_path", "")),
                    str(record.get("extraction_method", "")),
                    json.dumps(warnings, default=str),
                )
            )

        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT INTO pdf_quarantine "
                    "(quarantined_at, job_id, document_id, source_name, file_path, extraction_method, warnings) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
        return len(rows)

    def list_pdf_quarantine_records(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """Return persisted quarantined PDF records for admin/API display."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM pdf_quarantine "
                "ORDER BY quarantined_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            items: list[dict] = []
            for row in rows:
                item = dict(row)
                try:
                    item["warnings"] = json.loads(item.get("warnings") or "[]")
                except (json.JSONDecodeError, TypeError):
                    item["warnings"] = []
                items.append(item)
            return items
        finally:
            conn.close()
