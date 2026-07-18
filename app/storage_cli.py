from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.storage import FileStore


APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or clean a stopped tempserver storage directory."
    )
    parser.add_argument(
        "command",
        choices=("check", "gc"),
        help="check validates and repairs manifest copies; gc also removes orphan data",
    )
    parser.add_argument("--storage-dir", required=True, type=Path)
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=60,
        help="minimum orphan age for gc (default: 60)",
    )
    args = parser.parse_args()

    if args.grace_seconds < 0:
        parser.error("--grace-seconds must be non-negative")

    store = FileStore(
        args.storage_dir,
        protected_paths=(APP_DIR, PROJECT_DIR),
    )
    try:
        result: dict[str, object] = {"manifest": store.manifest_summary()}
        if args.command == "gc":
            result["deleted"] = store.collect_garbage(args.grace_seconds)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
