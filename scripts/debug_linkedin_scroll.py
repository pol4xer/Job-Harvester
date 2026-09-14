"""Scroll only the LinkedIn Jobs search and count unique job IDs.

Select this file in the IDE and press Run.  It does not open vacancy details,
does not start Scrapy, and does not read or write SQLite.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path

from selenium.webdriver.support.ui import WebDriverWait

from job_harvester.browser import BrowserLaunchOptions, SeleniumBaseSessionProvider
from job_harvester.config import SearchFilters, load_config, resolve_project_path
from job_harvester.middlewares import SeleniumBaseMiddleware
from job_harvester.parsing import build_linkedin_search_url, extract_linkedin_job_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "searches.yaml"

# Editable IDE launch parameters.
SEARCH_PROFILE = "senior_python"
CUSTOM_QUERY: str | None = None
LOCATION = "Worldwide"
WORKPLACE_TYPES = ("remote",)
SCROLL_ROUNDS = 50
HEADLESS = False
INCOGNITO = True
ROUND_PAUSE_MIN_SECONDS = 1.5
ROUND_PAUSE_MAX_SECONDS = 2.5
ROUND_SETTLE_TIMEOUT_SECONDS = 6.0
KEEP_BROWSER_OPEN_SECONDS = 0
PRINT_JOB_IDS = False


@dataclass(frozen=True, slots=True)
class ScrollDiagnosticResult:
    requested_rounds: int
    completed_rounds: int
    initial_unique_jobs: int
    total_unique_jobs: int
    rounds_with_growth: int
    show_more_clicks: int
    viewed_all_observations: int
    current_url: str


@dataclass(frozen=True, slots=True)
class DomJobSnapshot:
    raw_urn_nodes: int
    job_anchors: int
    numeric_job_ids: frozenset[str]


def _search_setup(
    *,
    profile_id: str,
    custom_query: str | None,
    location: str,
    workplace_types: tuple[str, ...],
):
    config, config_path = load_config(CONFIG_PATH)
    profile = config.get_profile(profile_id)
    query = custom_query.strip() if custom_query else profile.keywords
    filters = profile.filters or config.defaults.filters
    filters = SearchFilters.model_validate(
        {
            **filters.model_dump(),
            "workplace_types": list(workplace_types),
        }
    )
    search_url = build_linkedin_search_url(query, location, filters)
    return config, config_path, search_url


def _read_dom_job_snapshot(driver) -> DomJobSnapshot:
    payload = driver.execute_script(
        """
        const urnNodes = Array.from(document.querySelectorAll(
          '[data-entity-urn^="urn:li:jobPosting:"]'
        ));
        const anchors = Array.from(document.querySelectorAll(
          'a[href*="/jobs/view/"]'
        ));
        return {
          urns: urnNodes.map((node) => node.getAttribute('data-entity-urn') || ''),
          hrefs: anchors.map((node) => node.href || node.getAttribute('href') || ''),
        };
        """
    ) or {"urns": [], "hrefs": []}
    urns = payload.get("urns") or []
    hrefs = payload.get("hrefs") or []
    numeric_ids = {
        job_id
        for value in [*urns, *hrefs]
        if (job_id := extract_linkedin_job_id(value)) is not None
    }
    return DomJobSnapshot(
        raw_urn_nodes=len(urns),
        job_anchors=len(hrefs),
        numeric_job_ids=frozenset(numeric_ids),
    )


def _merge_visible_jobs(driver, cumulative_job_ids: set[str]) -> DomJobSnapshot:
    snapshot = _read_dom_job_snapshot(driver)
    cumulative_job_ids.update(snapshot.numeric_job_ids)
    return snapshot


def _page_height(driver) -> int:
    return int(
        driver.execute_script(
            "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);"
        )
        or 0
    )


def _wait_for_round_to_settle(
    driver,
    cumulative_job_ids: set[str],
    *,
    before_unique: int,
    before_height: int,
    minimum_pause_seconds: float,
    timeout_seconds: float,
) -> tuple[DomJobSnapshot, float, str]:
    started = time.monotonic()
    deadline = started + timeout_seconds
    last_state: tuple[int, int] | None = None
    stable_polls = 0
    snapshot = DomJobSnapshot(0, 0, frozenset())
    growth_observed = False

    while time.monotonic() < deadline:
        time.sleep(0.5)
        SeleniumBaseMiddleware._dismiss_sign_in_modal(driver)
        if SeleniumBaseMiddleware._is_blocked_page(driver):
            raise RuntimeError("LinkedIn displayed a login/security challenge while scrolling.")
        snapshot = _merge_visible_jobs(driver, cumulative_job_ids)
        current_height = _page_height(driver)
        state = (len(cumulative_job_ids), current_height)
        if len(cumulative_job_ids) > before_unique or current_height > before_height:
            growth_observed = True
        if state == last_state:
            stable_polls += 1
        else:
            last_state = state
            stable_polls = 0
        elapsed = time.monotonic() - started
        if elapsed >= minimum_pause_seconds and growth_observed and stable_polls >= 2:
            return snapshot, elapsed, "growth-settled"
    return snapshot, time.monotonic() - started, "timeout-no-growth"


def run_scroll_diagnostic(
    *,
    profile_id: str = SEARCH_PROFILE,
    custom_query: str | None = CUSTOM_QUERY,
    location: str = LOCATION,
    workplace_types: tuple[str, ...] = WORKPLACE_TYPES,
    scroll_rounds: int = SCROLL_ROUNDS,
    headless: bool = HEADLESS,
    incognito: bool = INCOGNITO,
    keep_browser_open_seconds: float = KEEP_BROWSER_OPEN_SECONDS,
    print_job_ids: bool = PRINT_JOB_IDS,
) -> ScrollDiagnosticResult:
    """Run an isolated search-only scroll diagnostic."""

    if scroll_rounds < 1:
        raise ValueError("scroll_rounds must be positive")
    config, config_path, search_url = _search_setup(
        profile_id=profile_id,
        custom_query=custom_query,
        location=location,
        workplace_types=workplace_types,
    )
    profile_dir = resolve_project_path(config.browser.profile_dir, config_path)
    options = BrowserLaunchOptions(
        headless=headless,
        incognito=incognito,
        use_uc=config.browser.use_uc,
        profile_dir=str(profile_dir),
        locale_code=config.browser.locale_code,
        window_size=config.browser.window_size,
        page_load_timeout=config.browser.page_load_timeout,
    )

    cumulative_job_ids: set[str] = set()
    rounds_with_growth = 0
    show_more_clicks = 0
    viewed_all_observations = 0
    completed_rounds = 0

    print("LinkedIn search-only scroll diagnostic")
    print(f"URL: {search_url}")
    print(f"Scroll rounds: {scroll_rounds}")
    print(f"Headless: {headless}; Incognito: {incognito}")
    print("SQLite: disabled; vacancy detail requests: disabled")

    with SeleniumBaseSessionProvider(options) as browser:
        browser.open_url(search_url)
        driver = browser.driver
        assert driver is not None
        WebDriverWait(driver, config.browser.wait_timeout).until(
            lambda current: (
                _read_dom_job_snapshot(current).numeric_job_ids
                or SeleniumBaseMiddleware._is_empty_search_page(current)
                or SeleniumBaseMiddleware._is_blocked_page(current)
            )
        )
        SeleniumBaseMiddleware._dismiss_sign_in_modal(driver)
        if SeleniumBaseMiddleware._is_blocked_page(driver):
            raise RuntimeError("LinkedIn displayed a login/security challenge.")

        initial_snapshot = _merge_visible_jobs(driver, cumulative_job_ids)
        initial_unique = len(cumulative_job_ids)
        print(
            f"Initial DOM: URNs={initial_snapshot.raw_urn_nodes}, "
            f"anchors={initial_snapshot.job_anchors}, unique jobs={initial_unique}"
        )

        for round_number in range(1, scroll_rounds + 1):
            before_unique = len(cumulative_job_ids)
            before_height = _page_height(driver)
            viewed_all = SeleniumBaseMiddleware._viewed_all_jobs(driver)
            show_more_clicked = SeleniumBaseMiddleware._click_show_more(driver)
            if viewed_all:
                viewed_all_observations += 1
            if show_more_clicked:
                show_more_clicks += 1

            driver.execute_script(
                "window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'});"
            )
            snapshot, waited_seconds, wait_outcome = _wait_for_round_to_settle(
                driver,
                cumulative_job_ids,
                before_unique=before_unique,
                before_height=before_height,
                minimum_pause_seconds=random.uniform(
                    ROUND_PAUSE_MIN_SECONDS,
                    ROUND_PAUSE_MAX_SECONDS,
                ),
                timeout_seconds=ROUND_SETTLE_TIMEOUT_SECONDS,
            )
            completed_rounds = round_number
            after_unique = len(cumulative_job_ids)
            added = after_unique - before_unique
            if added:
                rounds_with_growth += 1
            after_height = _page_height(driver)
            print(
                f"Round {round_number:02d}/{scroll_rounds}: "
                f"unique={after_unique} (+{added}), "
                f"URNs={snapshot.raw_urn_nodes}, anchors={snapshot.job_anchors}, "
                f"show_more={'clicked' if show_more_clicked else 'no'}, "
                f"viewed_all={'yes' if viewed_all else 'no'}, "
                f"height={before_height}->{after_height}, "
                f"wait={waited_seconds:.1f}s/{wait_outcome}"
            )

        if print_job_ids:
            print("\nUnique identities:")
            for identity in sorted(cumulative_job_ids):
                print(identity)

        if keep_browser_open_seconds > 0:
            print(f"Keeping browser open for {keep_browser_open_seconds:.0f}s...")
            time.sleep(keep_browser_open_seconds)
        current_url = driver.current_url

    result = ScrollDiagnosticResult(
        requested_rounds=scroll_rounds,
        completed_rounds=completed_rounds,
        initial_unique_jobs=initial_unique,
        total_unique_jobs=len(cumulative_job_ids),
        rounds_with_growth=rounds_with_growth,
        show_more_clicks=show_more_clicks,
        viewed_all_observations=viewed_all_observations,
        current_url=current_url,
    )
    print("\n" + "=" * 72)
    print(f"COMPLETED SCROLL ROUNDS: {result.completed_rounds}/{result.requested_rounds}")
    print(f"UNIQUE JOBS COLLECTED: {result.total_unique_jobs}")
    print(f"NEW AFTER INITIAL PAGE: {result.total_unique_jobs - result.initial_unique_jobs}")
    print(f"ROUNDS WITH GROWTH: {result.rounds_with_growth}")
    print(f"SHOW MORE CLICKS: {result.show_more_clicks}")
    print(f"VIEWED ALL OBSERVATIONS: {result.viewed_all_observations}")
    print("=" * 72)
    return result


def main() -> None:
    run_scroll_diagnostic()


if __name__ == "__main__":
    main()
