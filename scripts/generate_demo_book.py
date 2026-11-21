"""Generate synthetic demo book pages into a local directory."""

from __future__ import annotations

import argparse
from pathlib import Path

from book_cleaner.demo_generator import write_demo_book_to_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic demo paper book scans.")
    parser.add_argument("--out-dir", type=Path, default=Path("./data/demo_scans"))
    args = parser.parse_args()

    files = write_demo_book_to_dir(args.out_dir)
    print(f"Generated {len(files)} demo book scans in {args.out_dir.resolve()}:")
    for f in files:
        print(f"  - {f.name}")


if __name__ == "__main__":
    main()
