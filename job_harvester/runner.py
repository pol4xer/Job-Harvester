from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from job_harvester.config import DEFAULT_CONFIG, load_config, resolve_project_path
from job_harvester.sources import get_source


@dataclass(frozen=True, slots=True)
class CrawlResult:
    """Paths used by a completed programmatic crawl."""

    config_path: Path
    database_path: Path
    profile_dir: Path | None
    source: str
    items_processed: int


def run_crawl(
    *,
    source: str = "linkedin",
    input_path: str | Path | None = None,
    log_level: str | None = None,
    profile: str = "senior_python",
    query: str | None = None,
    location: str | None = None,
    workplace_types: Sequence[str] | None = None,
    max_results: int | None = None,
    max_scroll_rounds: int | None = None,
    refresh_existing: bool | None = None,
    headless: bool | None = None,
    incognito: bool | None = None,
    debug_pause_after_first_request_seconds: float | None = None,
    job_urls: Sequence[str] | None = None,
    database_path: str | Path | None = None,
    config_path: str | Path | None = DEFAULT_CONFIG,
) -> CrawlResult:
    """Run one registered source through the shared SQLite item pipeline.

    LinkedIn models geography and workplace separately.  Use
    ``location="Worldwide"`` together with
    ``workplace_types=("remote",)`` for a global Remote search.
    Search discovery stops when it reaches either ``max_results`` unique jobs
    or ``max_scroll_rounds`` real bottom-scroll actions, whichever comes first.
    With ``refresh_existing=False``, jobs that already have a complete stored
    description are discovered but do not receive another detail request.

    Twisted's reactor is intentionally started and stopped here, so call this
    function once per Python process.  ``run.py`` is the normal entry point.
    """

    definition = get_source(source)
    if definition.requires_input and input_path is None:
        raise ValueError(f"Source {source!r} requires a local --input JSONL file")
    resolved_input_path = Path(input_path).expanduser().resolve() if input_path else None
    if resolved_input_path is not None and not resolved_input_path.is_file():
        raise ValueError("Input must be an existing local file")
    spider_class = definition.load_spider()
    config, resolved_config_path = load_config(config_path)
    if source == "linkedin":
        config.get_profile(profile)

    resolved_database_path = (
        Path(database_path).expanduser().resolve()
        if database_path is not None
        else resolve_project_path(config.database, resolved_config_path)
    )
    profile_dir = (
        resolve_project_path(config.browser.profile_dir, resolved_config_path)
        if definition.requires_browser
        else None
    )
    resolved_database_path.parent.mkdir(parents=True, exist_ok=True)
    if profile_dir is not None:
        profile_dir.mkdir(parents=True, exist_ok=True)

    browser_headless = config.browser.headless if headless is None else headless
    browser_incognito = config.browser.incognito if incognito is None else incognito
    should_refresh_existing = (
        config.refresh_existing if refresh_existing is None else refresh_existing
    )
    scroll_round_limit = (
        config.browser.max_scroll_rounds if max_scroll_rounds is None else int(max_scroll_rounds)
    )
    if not 1 <= scroll_round_limit <= 5000:
        raise ValueError("max_scroll_rounds must be between 1 and 5000")
    debug_pause_seconds = (
        config.browser.debug_pause_after_first_request_seconds
        if debug_pause_after_first_request_seconds is None
        else float(debug_pause_after_first_request_seconds)
    )
    if debug_pause_seconds < 0:
        raise ValueError("debug pause must be zero or greater")
    os.environ.setdefault("SCRAPY_SETTINGS_MODULE", "job_harvester.settings")

    # Keep Scrapy imports inside the function so importing run.py has no reactor
    # side effects and remains friendly to IDE execution.
    from scrapy.crawler import CrawlerProcess
    from scrapy.utils.project import get_project_settings

    settings = get_project_settings()
    if log_level is not None:
        settings.set("LOG_LEVEL", log_level, priority="cmdline")
    if not definition.requires_browser:
        settings.set(
            "DOWNLOADER_MIDDLEWARES",
            {"job_harvester.middlewares.SeleniumBaseMiddleware": None},
            priority="project",
        )
    settings.set("JOB_DATABASE", str(resolved_database_path), priority="cmdline")
    settings.set("JOB_HARVESTER_DATABASE", str(resolved_database_path), priority="cmdline")
    settings.set("SELENIUM_BROWSER_PROFILE_DIR", str(profile_dir), priority="cmdline")
    settings.set("SELENIUM_BROWSER_HEADLESS", browser_headless, priority="cmdline")
    settings.set("SELENIUM_BROWSER_INCOGNITO", browser_incognito, priority="cmdline")
    settings.set("SELENIUM_BROWSER_USE_UC", config.browser.use_uc, priority="cmdline")
    settings.set("SELENIUM_BROWSER_LOCALE_CODE", config.browser.locale_code, priority="cmdline")
    settings.set("SELENIUM_BROWSER_WINDOW_SIZE", config.browser.window_size, priority="cmdline")
    settings.set(
        "SELENIUM_BROWSER_PAGE_LOAD_TIMEOUT",
        config.browser.page_load_timeout,
        priority="cmdline",
    )
    settings.set(
        "SELENIUM_BROWSER_WAIT_TIMEOUT",
        config.browser.wait_timeout,
        priority="cmdline",
    )
    settings.set(
        "SELENIUM_BROWSER_SCROLL_PAUSE_MIN",
        config.browser.scroll_pause_min,
        priority="cmdline",
    )
    settings.set(
        "SELENIUM_BROWSER_SCROLL_PAUSE_MAX",
        config.browser.scroll_pause_max,
        priority="cmdline",
    )
    settings.set(
        "SELENIUM_BROWSER_STAGNATION_LIMIT",
        config.browser.stagnation_limit,
        priority="cmdline",
    )
    settings.set(
        "SELENIUM_BROWSER_MAX_SCROLL_ROUNDS",
        scroll_round_limit,
        priority="cmdline",
    )
    settings.set(
        "SELENIUM_BROWSER_SEARCH_DEADLINE_SECONDS",
        config.browser.search_deadline_seconds,
        priority="cmdline",
    )
    settings.set(
        "SELENIUM_BROWSER_DEBUG_PAUSE_AFTER_FIRST_REQUEST_SECONDS",
        debug_pause_seconds,
        priority="cmdline",
    )

    process = CrawlerProcess(settings)
    crawler = process.create_crawler(spider_class)
    failures = []
    deferred = process.crawl(
        crawler,
        config_path=str(resolved_config_path),
        profile=profile,
        input_path=str(resolved_input_path) if resolved_input_path else None,
        query=query,
        location=location,
        workplace_types=list(workplace_types) if workplace_types is not None else None,
        max_results=max_results,
        max_scroll_rounds=scroll_round_limit,
        refresh_existing=should_refresh_existing,
        job_urls=list(job_urls or ()),
    )
    # Scrapy may consume startup failures or close with an unsuccessful reason
    # without raising from start(). Preserve both failure paths for callers/CI.
    deferred.addErrback(lambda failure: failures.append(failure))
    process.start(install_signal_handlers=False)
    if failures:
        failures[0].raiseException()
    stats = crawler.stats.get_stats()
    reason = getattr(crawler.spider, "close_reason", None) or stats.get("finish_reason")
    errors = sum(
        int(stats.get(key, 0))
        for key in ("job_harvester/errors", "job_harvester/source_errors", "log_count/ERROR")
    )
    if reason != "finished" or errors:
        raise RuntimeError(f"Source {source!r} did not finish successfully ({reason or 'unknown'})")
    return CrawlResult(
        config_path=resolved_config_path,
        database_path=resolved_database_path,
        profile_dir=profile_dir,
        source=source,
        items_processed=int(stats.get("job_harvester/items_processed", 0)),
    )
