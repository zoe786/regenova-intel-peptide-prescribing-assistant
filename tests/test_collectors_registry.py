from __future__ import annotations

from pathlib import Path

from collectors.registry import load_source_registry


def test_load_source_registry_yaml(tmp_path: Path) -> None:
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

    registry = load_source_registry(registry_file)

    assert len(registry.sources) == 1
    source = registry.sources[0]
    assert source.id == "website-source"
    assert source.type == "website"
    assert source.config["urls"] == ["https://example.com/a"]
