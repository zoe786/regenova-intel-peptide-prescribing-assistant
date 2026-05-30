from __future__ import annotations

from pathlib import Path

from collectors.models import CollectionArtifact
from collectors.state import CollectionStateStore


def test_collection_state_roundtrip(tmp_path: Path) -> None:
    state_path = tmp_path / "collectors_state.json"
    store = CollectionStateStore(state_path)

    artifact = CollectionArtifact(
        path="websites/urls_example.txt",
        content_hash="abc123",
        changed=True,
        record_count=2,
    )
    store.update_source("website-source", [artifact])
    store.save()

    reloaded = CollectionStateStore(state_path)

    assert reloaded.state.sources["website-source"].artifact_hashes[artifact.path] == "abc123"
    assert reloaded.state.sources["website-source"].last_collected_at is not None
