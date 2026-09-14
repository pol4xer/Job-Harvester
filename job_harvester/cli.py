from __future__ import annotations

import argparse
import importlib.util
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from job_harvester.config import DEFAULT_CONFIG, load_config, resolve_project_path
from job_harvester.runner import run_crawl
from job_harvester.sources import list_sources


def _config_path(value: str | None) -> Path:
    return Path(value or DEFAULT_CONFIG).expanduser().resolve()


def command_list_sources(args) -> int:
    for source in list_sources():
        print(f"{source.slug:16} {source.description}")
    return 0


def command_demo(args) -> int:
    from job_harvester.demo import run_demo

    result = run_demo(database_path=args.database, output_path=args.output)
    print("Offline demo complete. All records are fictional; no network or browser was used.")
    print(
        f"Processed: {result.items_processed} records; exported: {result.exported_jobs} unique jobs"
    )
    print(f"Database: {result.database_path}")
    print(f"Export:   {result.output_path}")
    return 0


def command_list_profiles(args) -> int:
    config, _ = load_config(_config_path(args.config))
    for profile in config.searches:
        state = "enabled" if profile.enabled else "disabled"
        location = profile.location or config.defaults.location
        limit = profile.max_results or config.defaults.max_results
        print(f"{profile.id:24} {state:8} limit={limit:<5} location={location}")
        print(f"  {profile.keywords}")
    return 0


def command_crawl(args) -> int:
    run_crawl(
        source=args.source,
        input_path=args.input,
        database_path=args.database,
        config_path=_config_path(args.config),
        profile=args.profile,
        query=args.query,
        location=args.location,
        max_results=args.max_results,
        max_scroll_rounds=args.max_scroll_rounds,
        refresh_existing=args.refresh_existing,
        job_urls=args.job_urls,
        headless=args.headless,
    )
    return 0


def command_login(args) -> int:
    config, config_path = load_config(_config_path(args.config))
    profile_dir = resolve_project_path(config.browser.profile_dir, config_path)
    profile_dir.mkdir(parents=True, exist_ok=True)

    from job_harvester.browser import BrowserLaunchOptions, SeleniumBaseSessionProvider

    options = BrowserLaunchOptions(
        headless=False,
        use_uc=config.browser.use_uc,
        profile_dir=str(profile_dir),
        locale_code=config.browser.locale_code,
        window_size=config.browser.window_size,
        page_load_timeout=config.browser.page_load_timeout,
    )
    with SeleniumBaseSessionProvider(options) as browser:
        browser.open_url("https://www.linkedin.com/login")
        print("Chrome is open with a separate persistent browser profile.")
        print(
            "Sign in manually. Chrome stores session cookies in the local profile; keep it private."
        )
        if args.wait_seconds:
            time.sleep(args.wait_seconds)
        else:
            try:
                input("After signing in, press Enter to close Chrome... ")
            except EOFError:
                print("No interactive input is available; Chrome will remain open for 60 seconds.")
                time.sleep(60)
    print(f"Local browser profile: {profile_dir}")
    return 0


def command_export(args) -> int:
    config, config_path = load_config(_config_path(args.config))
    database_path = (
        Path(args.database).expanduser().resolve()
        if args.database
        else resolve_project_path(config.database, config_path)
    )
    suffix = "csv" if args.format in {"csv", "tracker-csv", "tracker-csv-ru"} else args.format
    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else Path.cwd()
        / "data"
        / "exports"
        / f"vacancies_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.{suffix}"
    )

    from job_harvester.export import export_database

    count = export_database(
        database_path,
        output_path,
        format=args.format,
        limit=args.limit,
        since=args.since,
        complete_only=args.complete_only,
        include_raw=args.include_raw,
    )
    print(f"Exported {count} unique vacancies to {output_path}")
    return 0


def command_stats(args) -> int:
    config, config_path = load_config(_config_path(args.config))
    database_path = (
        Path(args.database).expanduser().resolve()
        if args.database
        else resolve_project_path(config.database, config_path)
    )
    if not database_path.exists():
        print(f"Database does not exist yet: {database_path}")
        return 0

    # Open the existing file normally, then enforce query-only mode.  A strict
    # ``mode=ro`` URI cannot initialise WAL/SHM sidecars after a portable
    # database copy, which makes an otherwise healthy database unreadable.
    connection = sqlite3.connect(database_path, timeout=30.0)
    connection.execute("PRAGMA query_only = ON")
    try:
        values = {
            "unique_jobs": connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "snapshots": connection.execute("SELECT COUNT(*) FROM job_snapshots").fetchone()[0],
            "search_runs": connection.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0],
            "search_hits": connection.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0],
        }
    finally:
        connection.close()
    print(f"Database: {database_path}")
    for name, value in values.items():
        print(f"{name:16} {value}")
    return 0


def command_doctor(args) -> int:
    config, config_path = load_config(_config_path(args.config))
    database_path = resolve_project_path(config.database, config_path)
    profile_dir = resolve_project_path(config.browser.profile_dir, config_path)
    chrome_candidates = [
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    chrome = next((Path(item) for item in chrome_candidates if item and Path(item).exists()), None)
    modules = {
        "scrapy": importlib.util.find_spec("scrapy") is not None,
        "seleniumbase": importlib.util.find_spec("seleniumbase") is not None,
        "pydantic": importlib.util.find_spec("pydantic") is not None,
        "yaml": importlib.util.find_spec("yaml") is not None,
    }
    print(f"Python:       {sys.version.split()[0]}")
    print(f"Config:       {config_path}")
    print(f"Database:     {database_path}")
    print(f"Browser data: {profile_dir}")
    print(f"Chrome:       {chrome or 'NOT FOUND'}")
    for module, available in modules.items():
        print(f"{module + ':':13} {'OK' if available else 'MISSING'}")
    okay = chrome is not None and all(modules.values())
    print(
        "Doctor result: OK"
        if okay
        else "Doctor result: install missing requirements with `uv sync`"
    )
    return 0 if okay else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-harvester",
        description="Collect job vacancies with modular source adapters, SQLite snapshots and exports.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sources = subparsers.add_parser("list-sources", help="Show implemented source adapters")
    sources.set_defaults(handler=command_list_sources)

    demo = subparsers.add_parser("demo", help="Import and export fictional offline demo records")
    demo.add_argument("--database", help="SQLite destination (default: data/demo/jobs.sqlite3)")
    demo.add_argument("--output", "-o", help="JSON export (default: data/demo/vacancies.json)")
    demo.set_defaults(handler=command_demo)

    profiles = subparsers.add_parser("list-profiles", help="Show configured short searches")
    profiles.add_argument("--config", default=str(DEFAULT_CONFIG))
    profiles.set_defaults(handler=command_list_profiles)

    crawl = subparsers.add_parser("crawl", help="Run one registered source adapter")
    crawl.add_argument("--source", default="linkedin", help="Source slug; see list-sources")
    crawl.add_argument("--input", help="Local JSONL file (required by the jsonl adapter)")
    crawl.add_argument("--database", help="Override the SQLite destination")
    crawl.add_argument("--config", default=str(DEFAULT_CONFIG))
    crawl.add_argument("--profile", default="senior_python")
    crawl.add_argument("--query", help="Override the profile Boolean query literally")
    crawl.add_argument("--location", help="Override LinkedIn location")
    crawl.add_argument("--max-results", type=int)
    crawl.add_argument(
        "--max-scroll-rounds",
        type=int,
        help="Maximum real bottom-scroll actions (default: config value)",
    )
    crawl.add_argument(
        "--refresh-existing",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Fetch complete jobs again; default comes from config (false)",
    )
    crawl.add_argument(
        "--job-url",
        dest="job_urls",
        action="append",
        help="Fetch one LinkedIn job URL directly; repeat for multiple URLs",
    )
    visibility = crawl.add_mutually_exclusive_group()
    visibility.add_argument("--headless", dest="headless", action="store_true")
    visibility.add_argument("--headed", dest="headless", action="store_false")
    crawl.set_defaults(handler=command_crawl, headless=None)

    login = subparsers.add_parser("login", help="Open Chrome for a manual LinkedIn login")
    login.add_argument("--config", default=str(DEFAULT_CONFIG))
    login.add_argument("--wait-seconds", type=int, default=0)
    login.set_defaults(handler=command_login)

    export = subparsers.add_parser("export", help="Export the latest snapshot per unique job")
    export.add_argument("--config", default=str(DEFAULT_CONFIG))
    export.add_argument("--database")
    export.add_argument("--output", "-o")
    export.add_argument(
        "--format",
        choices=("jsonl", "json", "csv", "tracker-csv", "tracker-csv-ru"),
        default="jsonl",
    )
    export.add_argument("--limit", type=int)
    export.add_argument("--since", help="ISO date or timestamp")
    export.add_argument("--complete-only", action="store_true")
    export.add_argument("--include-raw", action="store_true")
    export.set_defaults(handler=command_export)

    stats = subparsers.add_parser("stats", help="Show SQLite record counts")
    stats.add_argument("--config", default=str(DEFAULT_CONFIG))
    stats.add_argument("--database")
    stats.set_defaults(handler=command_stats)

    doctor = subparsers.add_parser("doctor", help="Check local runtime prerequisites")
    doctor.add_argument("--config", default=str(DEFAULT_CONFIG))
    doctor.set_defaults(handler=command_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args) or 0)
    except (ValueError, KeyError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
