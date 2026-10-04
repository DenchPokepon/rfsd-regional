"""Pinned input versions; no data files are included in the package."""

import hashlib
import json
from pathlib import Path

SOURCES = json.loads(Path(__file__).with_name("sources.json").read_text(encoding="utf-8"))


def verify_file(path: Path, expected_sha256: str) -> None:
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"Source version mismatch: {path}. Expected {expected_sha256}, got {actual}")
