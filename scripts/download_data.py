"""
Download all RFSD parquet files (2011-2025) from Hugging Face into data/RFSD.
Super laconic, fast multi-threaded download using huggingface_hub.
"""
import sys

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
from huggingface_hub import snapshot_download

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

def main():
    print(f"Target data directory: {DATA_DIR}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    print("Starting download of RFSD dataset (2011-2025)...")
    downloaded_path = snapshot_download(
        repo_id="irlspbru/RFSD",
        repo_type="dataset",
        allow_patterns="RFSD/**/*.parquet",
        local_dir=str(DATA_DIR),
        max_workers=4,
    )
    print(f"Downloaded files to: {downloaded_path}")
    
    # List downloaded parquet files and their sizes
    rfsd_dir = DATA_DIR / "RFSD"
    files = sorted(rfsd_dir.glob("year=*/part-0.parquet"))
    total_bytes = 0
    print("\nSummary of local RFSD parquet partitions:")
    print("-" * 45)
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        total_bytes += f.stat().st_size
        print(f"  {f.parent.name:<12} | {size_mb:>7.1f} MB | {f.name}")
    print("-" * 45)
    print(f"Total: {len(files)} partitions, {total_bytes / (1024**3):.2f} GB\n")

if __name__ == "__main__":
    main()
