from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import scrapy

from job_harvester.config import SearchFilters, load_config
from job_harvester.items import Vacancy
from job_harvester.parsing import (
    build_linkedin_search_url,
    canonical_linkedin_job_url,
    extract_linkedin_job_id,
    parse_date,
    parse_job_detail,
)
from job_harvester.request import SeleniumBrowserRequest
from job_harvester.storage import canonicalize_url, load_complete_job_identities


def _boolean_argument(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected a boolean value, got {value!r}")


class LinkedInJobsSpider(scrapy.Spider):
    name = "linkedin_jobs"
    source = "linkedin"

    def __init__(
        self,
        config_path: str | None = None,
        profile: str = "senior_python",
        query: str | None = None,
        location: str | None = None,
        workplace_types: list[str] | tuple[str, ...] | None = None,
        max_results: int | str | None = None,
        max_scroll_rounds: int | str | None = None,
        refresh_existing: bool | str | None = None,
        job_urls: list[str] | tuple[str, ...] | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        config, resolved_config_path = load_config(config_path)
        self.refresh_existing = (
            config.refresh_existing
            if refresh_existing is None
            else _boolean_argument(refresh_existing)
        )
        self.job_urls = [str(url).strip() for url in (job_urls or []) if str(url).strip()]
        search_profile = config.get_profile(profile)
        if not self.job_urls and not search_profile.enabled:
            raise ValueError(f"Search profile {profile!r} is disabled")

        self.config_path = resolved_config_path
        self.profile_id = "direct_urls" if self.job_urls else search_profile.id
        self.query = "" if self.job_urls else query.strip() if query else search_profile.keywords
        self.location = (
            ""
            if self.job_urls
            else location.strip()
            if location
            else search_profile.location or config.defaults.location
        )
        configured_filters = search_profile.filters or config.defaults.filters
        self.filters = (
            configured_filters
            if workplace_types is None
            else SearchFilters.model_validate(
                {
                    **configured_filters.model_dump(),
                    "workplace_types": list(workplace_types),
                }
            )
        )
        configured_limit = search_profile.max_results or config.defaults.max_results
        self.max_results = (
            len(self.job_urls)
            if self.job_urls
            else int(max_results)
            if max_results is not None
            else configured_limit
        )
        if self.max_results < 1:
            raise ValueError("max_results must be positive")
        self.max_scroll_rounds = (
            config.browser.max_scroll_rounds
            if max_scroll_rounds is None
            else int(max_scroll_rounds)
        )
        if not 1 <= self.max_scroll_rounds <= 5000:
            raise ValueError("max_scroll_rounds must be between 1 and 5000")

        self.search_url = (
            ""
            if self.job_urls
            else build_linkedin_search_url(self.query, self.location, self.filters)
        )
        self.run_id = str(uuid4())
        self.run_started_at = datetime.now(UTC)
        self.seen_job_ids: set[str] = set()
        # Search results arrive from the Selenium middleware in bounded JSON
        # chunks.  Keep every card in memory and do not schedule detail pages
        # until the search middleware reports that scrolling is fully done.
        self.discovered_search_cards: dict[str, dict] = {}
        self.collected_job_ids: set[str] = set()
        self.collected_job_urls: set[str] = set()
        self.skipped_existing = 0
        self.rank = 0
        self.search_session_key = f"{self.run_id}:{self.profile_id}"
        self.run_metadata = {
            "run_id": self.run_id,
            "source": "linkedin",
            "profile_id": self.profile_id,
            "query": self.query,
            "location": self.location,
            "max_results": self.max_results,
            "max_scroll_rounds": self.max_scroll_rounds,
            "refresh_existing": self.refresh_existing,
            "search_url": self.search_url,
            "direct_urls": self.job_urls,
            "config_path": str(self.config_path),
            "started_at": self.run_started_at.isoformat(),
        }

    async def start(self):
        self._load_collected_jobs()
        if self.job_urls:
            for rank, source_url in enumerate(self.job_urls, start=1):
                job_id = extract_linkedin_job_id(source_url)
                canonical_url = canonical_linkedin_job_url(job_id, source_url)
                identity = job_id or canonical_url
                if identity in self.seen_job_ids:
                    continue
                self.seen_job_ids.add(identity)
                skip_detail = self._skip_existing_detail(job_id, canonical_url)
                stub = Vacancy.model_validate(
                    {
                        "source": "linkedin",
                        "source_job_id": job_id,
                        "source_url": canonical_url,
                        "canonical_url": canonical_url,
                        "company": "Unknown company",
                        "title": "Unknown role",
                        "description": "",
                        "capture_status": "partial",
                        "captured_at": datetime.now(UTC),
                        "search_run_id": self.run_id,
                        "search_profile_id": self.profile_id,
                        "search_query": "",
                        "search_location": "",
                        "search_rank": rank,
                    }
                )
                yield stub.as_dict()
                if skip_detail:
                    continue
                yield SeleniumBrowserRequest(
                    canonical_url,
                    page_kind="detail",
                    callback=self.parse_detail,
                    errback=self.parse_detail_error,
                    meta={"vacancy_stub": stub.as_dict(), "handle_httpstatus_all": True},
                )
            self.logger.info(
                "Direct discovery finished with %s URLs (%s existing detail requests skipped)",
                len(self.seen_job_ids),
                self.skipped_existing,
            )
            return
        yield self._search_request(continue_scroll=False)

    def _load_collected_jobs(self) -> None:
        if self.refresh_existing:
            self.logger.info("refresh_existing=True: all discovered jobs will be requested")
            return
        settings = self.crawler.settings
        database_path = (
            settings.get("JOB_DATABASE")
            or settings.get("JOB_HARVESTER_DATABASE")
            or settings.get("JOB_HARVESTER_DB_PATH")
            or settings.get("SQLITE_DATABASE")
            or "data/jobs.sqlite3"
        )
        self.collected_job_ids, self.collected_job_urls = load_complete_job_identities(
            database_path
        )
        self.logger.info(
            "Loaded %s complete LinkedIn job IDs and %s canonical URLs from SQLite; "
            "their detail requests will be skipped",
            len(self.collected_job_ids),
            len(self.collected_job_urls),
        )

    def _skip_existing_detail(self, job_id: str | None, canonical_url: str) -> bool:
        normalized_url = canonicalize_url(canonical_url)
        already_collected = (job_id is not None and job_id in self.collected_job_ids) or (
            normalized_url is not None and normalized_url in self.collected_job_urls
        )
        if not already_collected:
            return False
        self.skipped_existing += 1
        self.crawler.stats.inc_value("job_harvester/jobs_skipped_existing")
        self.logger.debug("Skipping complete existing LinkedIn job %s", job_id or canonical_url)
        return True

    def _search_request(self, *, continue_scroll: bool):
        return SeleniumBrowserRequest(
            self.search_url,
            page_kind="search",
            callback=self.parse_search,
            errback=self.parse_search_error,
            dont_filter=True,
            priority=10_000,
            meta={
                "search_session_key": self.search_session_key,
                "continue_scroll": continue_scroll,
                "target_total": self.max_results,
                "max_scroll_rounds": self.max_scroll_rounds,
                "chunk_size": min(50, self.max_results),
            },
        )

    def parse_search(self, response):
        payload = json.loads(response.text)
        jobs = payload.get("jobs") or []
        for card in jobs:
            self._buffer_search_card(card)

        self.logger.info(
            "Discovery phase: received %s cards, buffered=%s unique, scrolls=%s/%s, stop_reason=%s",
            len(jobs),
            len(self.discovered_search_cards),
            payload.get("scrolls_completed"),
            self.max_scroll_rounds,
            payload.get("stop_reason"),
        )

        if payload.get("can_continue"):
            self.logger.info(
                "Discovery is not finished; scheduling only the next search chunk. "
                "No LinkedIn detail pages have been requested yet."
            )
            yield self._search_request(continue_scroll=True)
            return

        buffered_cards = list(self.discovered_search_cards.values())
        self.crawler.stats.set_value(
            "job_harvester/jobs_buffered_before_details", len(buffered_cards)
        )
        self.logger.info(
            "Discovery phase finished: %s unique job links are buffered in memory "
            "after %s/%s scrolls (stop_reason=%s). Starting detail phase now.",
            len(buffered_cards),
            payload.get("scrolls_completed"),
            self.max_scroll_rounds,
            payload.get("stop_reason"),
        )

        for card in buffered_cards:
            job_id = extract_linkedin_job_id(
                card.get("entity_urn"),
                card.get("href"),
                card.get("source_job_id"),
            )
            source_url = card.get("href")
            if not source_url:
                continue
            identity = job_id or source_url
            if identity in self.seen_job_ids:
                continue
            self.seen_job_ids.add(identity)
            self.rank += 1

            canonical_url = canonical_linkedin_job_url(job_id, source_url)
            skip_detail = self._skip_existing_detail(job_id, canonical_url)
            stub = {
                "source": "linkedin",
                "source_job_id": job_id,
                "source_url": canonical_url,
                "canonical_url": canonical_url,
                "company": card.get("company") or "Unknown company",
                "title": card.get("title") or "Unknown role",
                "description": "",
                "card_text": card.get("card_text") or "",
                "location": card.get("location") or None,
                "posted_at": parse_date(card.get("posted_at")),
                "capture_status": "partial",
                "captured_at": datetime.now(UTC),
                "search_run_id": self.run_id,
                "search_profile_id": self.profile_id,
                "search_query": self.query,
                "search_location": self.location,
                "search_rank": self.rank,
            }
            vacancy = Vacancy.model_validate(stub)
            yield vacancy.as_dict()
            if skip_detail:
                continue

            yield SeleniumBrowserRequest(
                canonical_url,
                page_kind="detail",
                callback=self.parse_detail,
                errback=self.parse_detail_error,
                priority=0,
                meta={"vacancy_stub": vacancy.as_dict(), "handle_httpstatus_all": True},
            )

        self.logger.info(
            "Detail phase queued for %s unique jobs (%s existing detail requests skipped; "
            "target %s)",
            self.rank,
            self.skipped_existing,
            self.max_results,
        )

    def _buffer_search_card(self, card: dict) -> None:
        source_url = card.get("href")
        job_id = extract_linkedin_job_id(
            card.get("entity_urn"),
            source_url,
            card.get("source_job_id"),
        )
        identity = job_id or canonicalize_url(source_url) or source_url
        if not identity or not source_url:
            return

        existing = self.discovered_search_cards.get(identity)
        if existing is None:
            self.discovered_search_cards[identity] = dict(card)
            return
        for key, value in card.items():
            if value and not existing.get(key):
                existing[key] = value

    def parse_detail(self, response):
        stub = dict(response.meta["vacancy_stub"])
        payload = parse_job_detail(response, stub)
        payload.update(
            {
                "search_run_id": self.run_id,
                "search_profile_id": self.profile_id,
                "search_query": self.query,
                "search_location": self.location,
                "search_rank": stub.get("search_rank"),
            }
        )
        yield Vacancy.model_validate(payload).as_dict()

    def parse_detail_error(self, failure):
        self.close_reason = "detail_failed"
        self.crawler.stats.inc_value("job_harvester/source_errors")
        request = failure.request
        stub = request.meta.get("vacancy_stub") or {}
        self.logger.warning(
            "Could not enrich LinkedIn job %s: %s. The partial card remains in SQLite.",
            stub.get("source_job_id") or request.url,
            failure.value,
        )

    def parse_search_error(self, failure):
        self.close_reason = "search_failed"
        self.crawler.stats.inc_value("job_harvester/source_errors")
        self.logger.error("LinkedIn search failed: %s", failure.value)


# Standalone IDE/debug launch data.  These values are intentionally small and
# write to a separate SQLite file, so a deployment smoke-run does not pollute
# the main data/jobs.sqlite3 database.
DEBUG_PROFILE = "senior_python"
DEBUG_QUERY = '"Senior Python Engineer" OR "Senior Python Developer"'
DEBUG_LOCATION = "Worldwide"
DEBUG_WORKPLACE_TYPES = ("remote",)
DEBUG_MAX_RESULTS = 10
DEBUG_MAX_SCROLL_ROUNDS = 40
DEBUG_REFRESH_EXISTING = False
DEBUG_HEADLESS = False
DEBUG_INCOGNITO = True
DEBUG_PAUSE_AFTER_FIRST_REQUEST_SECONDS = 2


def main() -> None:
    """Run this parser file directly for an interactive deployment smoke-test."""

    # Import locally: Scrapy also imports this module as the canonical spider
    # module, while this particular copy may be executing as ``__main__``.
    from job_harvester.runner import run_crawl

    debug_database = Path.cwd() / "data" / "debug_jobs.sqlite3"
    print("LinkedIn Jobs debug run")
    print("- headed Chrome")
    print("- Incognito without the persistent Selenium profile")
    print(f"- maximum search scrolls: {DEBUG_MAX_SCROLL_ROUNDS}")
    print(f"- refresh existing complete jobs: {DEBUG_REFRESH_EXISTING}")
    print(f"- {DEBUG_PAUSE_AFTER_FIRST_REQUEST_SECONDS}s pause after the first request")
    print(f"- SQLite: {debug_database}")

    result = run_crawl(
        profile=DEBUG_PROFILE,
        query=DEBUG_QUERY,
        location=DEBUG_LOCATION,
        workplace_types=DEBUG_WORKPLACE_TYPES,
        max_results=DEBUG_MAX_RESULTS,
        max_scroll_rounds=DEBUG_MAX_SCROLL_ROUNDS,
        refresh_existing=DEBUG_REFRESH_EXISTING,
        headless=DEBUG_HEADLESS,
        incognito=DEBUG_INCOGNITO,
        debug_pause_after_first_request_seconds=(DEBUG_PAUSE_AFTER_FIRST_REQUEST_SECONDS),
        database_path=debug_database,
    )
    print(f"Debug crawl finished. SQLite updated: {result.database_path}")


if __name__ == "__main__":
    main()
