"""CLI entrypoint for approved-source collectors."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

from collectors.runner import main

if __name__ == "__main__":
    main()
