"""Offline portfolio example using the normal adapter, SQLite pipeline and export."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from job_harvester.export import export_database
from job_harvester.runner import run_crawl

DEMO_INPUT = Path(__file__).resolve().parent / "resources" / "demo_jobs.jsonl"


@dataclass(frozen=True, slots=True)
class DemoResult:
    database_path: Path
    output_path: Path
    items_processed: int
    exported_jobs: int


def run_demo(
    *, database_path: str | Path | None = None, output_path: str | Path | None = None
) -> DemoResult:
    database = Path(database_path or "data/demo/jobs.sqlite3").expanduser().resolve()
    output = Path(output_path or "data/demo/vacancies.json").expanduser().resolve()
    if database == output:
        raise ValueError("Demo database and export must have different paths")
    crawl = run_crawl(
        source="jsonl", input_path=DEMO_INPUT, database_path=database, log_level="WARNING"
    )
    count = export_database(database, output, format="json")
    return DemoResult(database, output, crawl.items_processed, count)
