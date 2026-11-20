"""Shared companion storage path resolution."""

import os

DEFAULT_TRUTH_DB_PATH = "data/companion_truth.db"


def resolve_truth_db_path(path: str | None = None) -> str:
    return path or os.environ.get("MAIBOT_TRUTH_DB_PATH", "").strip() or DEFAULT_TRUTH_DB_PATH
