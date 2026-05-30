"""Source registry loader."""

from __future__ import annotations

from pathlib import Path

import yaml

from collectors.models import SourceRegistry


def load_source_registry(path: Path = Path("config/source_registry.yaml")) -> SourceRegistry:
    """Load source registry YAML into typed models."""
    registry_path = Path(path)
    if not registry_path.exists():
        raise FileNotFoundError(f"Source registry not found: {registry_path}")

    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    return SourceRegistry.model_validate(raw)
