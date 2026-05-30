"""Base collector abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from collectors.models import CollectionArtifact, CollectionResult, SourceDefinition
from collectors.utils import ensure_parent_dir, sha256_text


class BaseCollector(ABC):
    """Abstract base class for all source collectors."""

    def __init__(self, raw_root: Path = Path("data/raw")) -> None:
        self.raw_root = Path(raw_root)

    @abstractmethod
    def collect(self, source: SourceDefinition) -> CollectionResult:
        """Collect from approved source definition and write raw artifacts."""

    def write_text_artifact(self, relative_path: Path, content: str, record_count: int = 0) -> CollectionArtifact:
        """Write a UTF-8 text artifact under data/raw with change detection."""
        target = self.raw_root / relative_path
        ensure_parent_dir(target)
        new_hash = sha256_text(content)
        prior_hash: str | None = None
        if target.exists():
            prior_hash = sha256_text(target.read_text(encoding="utf-8"))

        changed = prior_hash != new_hash
        if changed:
            target.write_text(content, encoding="utf-8")

        return CollectionArtifact(
            path=str(relative_path.as_posix()),
            content_hash=new_hash,
            changed=changed,
            record_count=record_count,
        )

    @staticmethod
    def dedupe_lines(items: list[str]) -> list[str]:
        """Return clean unique non-empty lines preserving order."""
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            value = item.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out
