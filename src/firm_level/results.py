"""Preserve analysis runs and index their files; never overwrite an existing run."""

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .dataset import PROJECT_ROOT
from .sources import SOURCES


def create_run(label: str, directory: Path | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory or PROJECT_ROOT / "outputs" / "runs" / f"{stamp}-{label}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def finish_run(path: Path, label: str, metadata: dict) -> None:
    """Call after successful writes. An unfinished directory is not indexed."""
    path = path.resolve()
    code = [*PROJECT_ROOT.glob("scripts/*.py"), *PROJECT_ROOT.glob("src/firm_level/*.py"),
            PROJECT_ROOT / "uv.lock", PROJECT_ROOT / "docs/analysis_rules.md"]
    ref = status = None
    if shutil.which("git"):
        ref = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                             capture_output=True, text=True, check=False)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT,
                                capture_output=True, text=True, check=False)
    manifest = {
        "label": label, "saved_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": ref.stdout.strip() if ref and ref.returncode == 0 else None,
        "git_worktree_dirty": bool(status.stdout.strip()) if status and status.returncode == 0 else None,
        "python": sys.version.split()[0], "command": ["python", *sys.argv],
        "metadata": metadata, "sources": SOURCES,
        "code_sha256": {p.relative_to(PROJECT_ROOT).as_posix(): file_hash(p) for p in code if p.is_file()},
        "files": {p.relative_to(path).as_posix(): {"bytes": p.stat().st_size, "sha256": file_hash(p)}
                  for p in sorted(path.rglob("*")) if p.is_file() and p.name != "manifest.json"},
    }
    manifest_path = path / "manifest.json"
    if manifest_path.exists():
        # Adding a report must preserve the provenance of the original calculation.
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name, info in original["files"].items():
            if name not in manifest["files"] or manifest["files"][name] != info:
                raise ValueError(f"Saved result changed or disappeared: {name}")
        original["files"] = manifest["files"]
        manifest = original
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    index_path = PROJECT_ROOT / "outputs" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    try:
        location = path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        location = path.as_posix()
    entry = {"label": label, "path": location, "saved_utc": manifest["saved_utc"],
             "files": len(manifest["files"]), "manifest_sha256": file_hash(path / "manifest.json")}
    index = [item for item in index if item["path"] != location] + [entry]
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = index_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(index_path)
