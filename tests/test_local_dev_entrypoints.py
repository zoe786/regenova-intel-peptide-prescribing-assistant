"""Tests for local developer entrypoint robustness."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from pipelines import run_all_ingestion


def test_run_all_ingestion_can_insert_repo_root_on_sys_path(monkeypatch):
    repo_root = str(Path(run_all_ingestion.__file__).resolve().parents[1])
    monkeypatch.setattr(run_all_ingestion.sys, "path", ["/tmp/other"])

    run_all_ingestion._ensure_repo_root_on_path()

    assert run_all_ingestion.sys.path[0] == repo_root


def test_makefile_uses_tabs_for_targets():
    makefile = Path(__file__).parents[1] / "Makefile"
    lines = makefile.read_text(encoding="utf-8").splitlines()
    run_api_idx = lines.index("run-api:")
    ingest_all_idx = lines.index("ingest-all:")
    assert lines[run_api_idx + 1].startswith("\t")
    assert lines[ingest_all_idx + 1].startswith("\t")
    assert "-m pipelines.run_all_ingestion" in lines[ingest_all_idx + 1]


def test_init_db_script_runs_from_repo_root():
    repo_root = Path(__file__).parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/init_db.py"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
