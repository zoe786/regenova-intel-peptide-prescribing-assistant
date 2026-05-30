"""Structured audit logging for collector runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from collectors.utils import ensure_parent_dir, now_utc_iso


class CollectorAuditLogger:
    """Writes JSONL audit events under data/audit/collectors/."""

    def __init__(self, run_id: str, root_dir: Path = Path("data/audit/collectors")) -> None:
        self.run_id = run_id
        self.path = Path(root_dir) / f"{run_id}.jsonl"
        ensure_parent_dir(self.path)

    def log(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "timestamp": now_utc_iso(),
            "run_id": self.run_id,
            "event_type": event_type,
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
