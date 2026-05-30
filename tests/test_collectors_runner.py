from __future__ import annotations

from pathlib import Path

from collectors.runner import CollectionRunner


def test_runner_triggers_ingestion_only_when_artifacts_change(tmp_path: Path, monkeypatch) -> None:
    registry_file = tmp_path / "source_registry.yaml"
    registry_file.write_text(
        """
        sources:
          - id: website-source
            type: website
            enabled: true
            config:
              urls:
                - https://example.com/a
        """,
        encoding="utf-8",
    )

    called: list[str] = []

    def fake_trigger(self, source_type):
        called.append(source_type)

    monkeypatch.setattr(CollectionRunner, "_trigger_ingestion", fake_trigger)

    runner = CollectionRunner(
        registry_path=registry_file,
        raw_root=tmp_path / "raw",
        state_path=tmp_path / "state" / "collectors_state.json",
        audit_root=tmp_path / "audit",
    )

    first_summary = runner.run(trigger_ingestion=True)
    second_summary = runner.run(trigger_ingestion=True)

    assert first_summary.changed_sources == 1
    assert second_summary.changed_sources == 0
    assert called == ["website"]
