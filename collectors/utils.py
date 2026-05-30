"""Utility helpers for collectors."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_utc_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


def ensure_parent_dir(path: Path) -> None:
    """Create parent directory for a file path if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)


def sha256_text(value: str) -> str:
    """SHA-256 hash for textual artifact content."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_json_file(path: Path) -> Any:
    """Read JSON data from disk."""
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json_file(path: Path, payload: Any) -> None:
    """Write JSON data to disk with stable formatting."""
    ensure_parent_dir(path)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
