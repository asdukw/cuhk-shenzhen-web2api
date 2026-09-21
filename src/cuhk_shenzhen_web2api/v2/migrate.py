"""Read legacy JSON and import to a NEW database; never overwrite a report."""

import argparse
import json
from pathlib import Path

from .store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.source.is_dir():
        parser.error("source must be an existing legacy chat_session directory")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    with Store(args.output_dir / "runtime.sqlite3") as store:
        report = store.import_legacy(args.source)
    with (args.output_dir / "migration-report.json").open(
        "x", encoding="utf-8"
    ) as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print("Migration report:", args.output_dir / "migration-report.json")


if __name__ == "__main__":
    main()
