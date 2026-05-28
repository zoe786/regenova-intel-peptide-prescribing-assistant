from __future__ import annotations

import json
from pathlib import Path

from pipelines.ingest_websites import _load_auth_config


def test_load_auth_config_from_file(tmp_path: Path, monkeypatch) -> None:
    raw_dir = tmp_path / "websites"
    raw_dir.mkdir(parents=True)
    (raw_dir / "auth.json").write_text(
        json.dumps(
            {
                "domains": {
                    "WWW.Example.com": {
                        "headers": {"Authorization": "******"},
                        "cookies": {"sessionid": "xyz"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("WEBSITE_INGEST_AUTH_JSON", raising=False)

    config = _load_auth_config(raw_dir)

    assert "example.com" in config
    assert config["example.com"]["headers"]["Authorization"] == "******"
    assert config["example.com"]["cookies"]["sessionid"] == "xyz"


def test_env_auth_config_overrides_file(tmp_path: Path, monkeypatch) -> None:
    raw_dir = tmp_path / "websites"
    raw_dir.mkdir(parents=True)
    (raw_dir / "auth.json").write_text(
        json.dumps({"domains": {"example.com": {"headers": {"Authorization": "******"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "WEBSITE_INGEST_AUTH_JSON",
        json.dumps(
            {"domains": {"example.com": {"headers": {"Authorization": "******"}}}}
        ),
    )

    config = _load_auth_config(raw_dir)

    assert config["example.com"]["headers"]["Authorization"] == "******"
