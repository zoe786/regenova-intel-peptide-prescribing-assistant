"""File-backed state store for collector checkpoints."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from collectors.models import CollectionArtifact
from collectors.utils import dump_json_file, load_json_file, now_utc_iso


class SourceState(BaseModel):
    """Checkpoint details for one source."""

    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    last_collected_at: str | None = None


class CollectorState(BaseModel):
    """Checkpoint map for all sources."""

    sources: dict[str, SourceState] = Field(default_factory=dict)


class CollectionStateStore:
    """Simple JSON-backed state persistence."""

    def __init__(self, path: Path = Path("data/state/collectors_state.json")) -> None:
        self.path = Path(path)
        self.state = CollectorState()
        self.load()

    def load(self) -> CollectorState:
        if self.path.exists():
            self.state = CollectorState.model_validate(load_json_file(self.path))
        return self.state

    def save(self) -> None:
        dump_json_file(self.path, self.state.model_dump())

    def update_source(self, source_id: str, artifacts: list[CollectionArtifact]) -> None:
        checkpoint = self.state.sources.get(source_id, SourceState())
        for artifact in artifacts:
            checkpoint.artifact_hashes[artifact.path] = artifact.content_hash
        checkpoint.last_collected_at = now_utc_iso()
        self.state.sources[source_id] = checkpoint

    def has_artifact_changed(self, source_id: str, artifact: CollectionArtifact) -> bool:
        checkpoint = self.state.sources.get(source_id)
        if checkpoint is None:
            return True
        return checkpoint.artifact_hashes.get(artifact.path) != artifact.content_hash
