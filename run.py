"""Editable Python entry point for a global Remote LinkedIn search.

Select this file in the IDE and press Run.  No CLI argument construction is
required: edit the constants below and run the file again.
"""

from pathlib import Path

from job_harvester.export import export_database
from job_harvester.runner import run_crawl

PROJECT_ROOT = Path(__file__).resolve().parent

# Choose one ID from config/searches.yaml.  CUSTOM_QUERY overrides its keywords
# when it is not None.
SEARCH_PROFILE = "senior_python"
CUSTOM_QUERY: str | None = None

# Worldwide removes the country/city restriction.  Remote is a separate
# workplace filter (LinkedIn f_WT=2); together they mean global Remote.
LOCATION = "Worldwide"
WORKPLACE_TYPES = ("remote",)

MAX_RESULTS = 25
# Stop discovery at the first limit reached: unique jobs or real bottom scrolls.
MAX_SCROLL_ROUNDS = 40
# False: request details only for vacancies not already complete in SQLite.
# True: request every discovered vacancy again and store changed snapshots.
REFRESH_EXISTING = False
HEADLESS = False
INCOGNITO = True
DEBUG_PAUSE_AFTER_FIRST_REQUEST_SECONDS = 0
DIRECT_JOB_URLS: tuple[str, ...] = ()

EXPORT_AFTER_CRAWL = True
JSONL_OUTPUT = PROJECT_ROOT / "data" / "exports" / "vacancies.jsonl"
CSV_OUTPUT = PROJECT_ROOT / "data" / "exports" / "vacancies.csv"
TRACKER_CSV_OUTPUT = PROJECT_ROOT / "data" / "exports" / "vacancies_tracker.csv"


def main() -> None:
    print("Starting LinkedIn search: global geography, Remote only")
    print(f"Search limits: {MAX_RESULTS} unique jobs or {MAX_SCROLL_ROUNDS} scrolls")
    print(f"Refresh already complete jobs: {REFRESH_EXISTING}")
    result = run_crawl(
        profile=SEARCH_PROFILE,
        query=CUSTOM_QUERY,
        location=LOCATION,
        workplace_types=WORKPLACE_TYPES,
        max_results=MAX_RESULTS,
        max_scroll_rounds=MAX_SCROLL_ROUNDS,
        refresh_existing=REFRESH_EXISTING,
        headless=HEADLESS,
        incognito=INCOGNITO,
        debug_pause_after_first_request_seconds=DEBUG_PAUSE_AFTER_FIRST_REQUEST_SECONDS,
        job_urls=DIRECT_JOB_URLS,
        config_path=PROJECT_ROOT / "config" / "searches.yaml",
    )

    print(f"SQLite updated: {result.database_path}")
    if not EXPORT_AFTER_CRAWL:
        return

    jsonl_count = export_database(
        result.database_path,
        JSONL_OUTPUT,
        format="jsonl",
        complete_only=True,
    )
    csv_count = export_database(
        result.database_path,
        CSV_OUTPUT,
        format="csv",
        complete_only=True,
    )
    tracker_count = export_database(
        result.database_path,
        TRACKER_CSV_OUTPUT,
        format="tracker-csv",
        complete_only=True,
    )
    print(f"JSONL exported: {jsonl_count} jobs -> {JSONL_OUTPUT}")
    print(f"Full CSV exported: {csv_count} jobs -> {CSV_OUTPUT}")
    print(f"Tracker CSV exported: {tracker_count} jobs -> {TRACKER_CSV_OUTPUT}")


if __name__ == "__main__":
    main()
