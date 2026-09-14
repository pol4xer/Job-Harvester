#!/usr/bin/env python3
"""Editable IDE entry point for exporting the accumulated SQLite jobs."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from job_harvester.export import export_database  # noqa: E402

# Edit these values and run this file from the IDE.  No command-line arguments
# are required.
DATABASE_PATH = PROJECT_ROOT / "data" / "jobs.sqlite3"
EXPORT_TARGETS = (
    ("jsonl", PROJECT_ROOT / "data" / "exports" / "vacancies.jsonl"),
    # Normalized CSV includes the complete description and description_html.
    ("csv", PROJECT_ROOT / "data" / "exports" / "vacancies.csv"),
    # A compact 26-column application tracker is available separately.
    ("tracker-csv", PROJECT_ROOT / "data" / "exports" / "vacancies_tracker.csv"),
)
COMPLETE_ONLY = True
LIMIT: int | None = None
SINCE: str | None = None
INCLUDE_RAW = False


def main() -> None:
    """Export the configured files directly through the Python API."""

    print(f"Source SQLite: {DATABASE_PATH}")
    for export_format, output_path in EXPORT_TARGETS:
        count = export_database(
            DATABASE_PATH,
            output_path,
            format=export_format,
            limit=LIMIT,
            since=SINCE,
            complete_only=COMPLETE_ONLY,
            include_raw=INCLUDE_RAW,
        )
        print(f"{export_format}: {count} jobs -> {output_path}")


if __name__ == "__main__":
    main()
