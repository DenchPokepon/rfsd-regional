"""Download pinned sources, or verify an existing local copy."""

import argparse
import shutil
import urllib.request
from pathlib import Path

from rfsd_regional.dataset import DATA_DIR
from rfsd_regional.sources import SOURCES, verify_file


def download_rosstat(directory: Path, verify_only: bool = False) -> None:
    for name, meta in SOURCES["rosstat"].items():
        path = directory / name
        if path.exists() or verify_only:
            verify_file(path, meta["sha256"])
            continue
        temporary = path.with_suffix(".download")
        request = urllib.request.Request(meta["url"], headers={"User-Agent": "RFSD research"})
        try:
            # Default HTTPS certificate and hostname verification stays enabled.
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as stream:
                shutil.copyfileobj(response, stream)
            verify_file(temporary, meta["sha256"])
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def download_rfsd(directory: Path, verify_only: bool = False) -> None:
    meta = SOURCES["rfsd"]
    missing = []
    for name, spec in meta["files"].items():
        path = directory / name
        if path.exists() or verify_only:
            verify_file(path, spec["sha256"])
        else:
            missing.append(name)
    if missing:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=meta["repo_id"], repo_type="dataset", revision=meta["revision"],
            allow_patterns=missing, local_dir=directory, max_workers=4,
        )
        for name in missing:
            verify_file(directory / name, meta["files"][name]["sha256"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["rfsd", "rosstat", "all"], default="rfsd")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--verify-only", action="store_true", help="No network access; check local SHA256")
    args = parser.parse_args()
    if not args.verify_only:
        args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.source in {"rfsd", "all"}:
        download_rfsd(args.data_dir, args.verify_only)
    if args.source in {"rosstat", "all"}:
        download_rosstat(args.data_dir, args.verify_only)
    print(f"Verified {args.source}: {args.data_dir}")


if __name__ == "__main__":
    main()
