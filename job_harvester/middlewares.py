from __future__ import annotations

import json
import random
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from scrapy import signals
from scrapy.exceptions import CloseSpider
from scrapy.http import HtmlResponse, TextResponse
from scrapy.utils.defer import deferred_to_future
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from twisted.internet.threads import deferToThread

from job_harvester.browser import BrowserLaunchOptions, SeleniumBaseSessionProvider
from job_harvester.request import SELENIUM_META_KEY, SELENIUM_PAGE_KIND_META_KEY

JOB_ID_FROM_URL = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)")
JOB_ID_FROM_URN = re.compile(r"urn:li:jobPosting:(\d+)")
DETAIL_DESCRIPTION_SELECTORS = (
    "[id^='JobDetails_AboutTheJob_']",
    "#job-details",
    ".jobs-description-content__text",
    ".jobs-description__content",
    ".description .show-more-less-html__markup",
    ".show-more-less-html__markup",
)
DESCRIPTION_EXPAND_SELECTORS = (
    "button.show-more-less-html__button",
    "button[data-tracking-control-name*='show-more-less-html']",
)
DOM_DESCRIPTION_SETTLE_SECONDS = 2.0


@dataclass
class SearchScrollState:
    jobs: dict[str, dict] = field(default_factory=dict)
    returned_ids: set[str] = field(default_factory=set)
    exhausted: bool = False
    stagnation: int = 0
    scrolls_completed: int = 0
    stop_reason: str | None = None
    viewed_all_observed: bool = False
    started_monotonic: float = field(default_factory=time.monotonic)


class SeleniumBaseMiddleware:
    """Render marked requests in one persistent SeleniumBase Chrome session."""

    def __init__(self, crawler):
        self.crawler = crawler
        settings = crawler.settings
        self.browser = SeleniumBaseSessionProvider(BrowserLaunchOptions.from_settings(settings))
        self.wait_timeout = settings.getint("SELENIUM_BROWSER_WAIT_TIMEOUT", 20)
        self.pause_min = settings.getfloat("SELENIUM_BROWSER_SCROLL_PAUSE_MIN", 1.0)
        self.pause_max = settings.getfloat("SELENIUM_BROWSER_SCROLL_PAUSE_MAX", 2.0)
        self.stagnation_limit = settings.getint("SELENIUM_BROWSER_STAGNATION_LIMIT", 3)
        self.max_scroll_rounds = settings.getint("SELENIUM_BROWSER_MAX_SCROLL_ROUNDS", 250)
        self.search_deadline_seconds = settings.getint(
            "SELENIUM_BROWSER_SEARCH_DEADLINE_SECONDS", 900
        )
        self.debug_pause_after_first_request_seconds = settings.getfloat(
            "SELENIUM_BROWSER_DEBUG_PAUSE_AFTER_FIRST_REQUEST_SECONDS", 0
        )
        self._first_request_debug_pause_done = False
        self.artifact_dir = (
            Path(settings.get("SELENIUM_BROWSER_ARTIFACT_DIR", ".artifacts")).expanduser().resolve()
        )
        self.search_states: dict[str, SearchScrollState] = {}

    @classmethod
    def from_crawler(cls, crawler):
        middleware = cls(crawler)
        crawler.signals.connect(middleware.spider_closed, signal=signals.spider_closed)
        return middleware

    async def process_request(self, request):
        if not request.meta.get(SELENIUM_META_KEY):
            return None
        spider = self.crawler.spider
        return await deferred_to_future(deferToThread(self._render_request, request, spider))

    def _render_request(self, request, spider):
        page_kind = request.meta.get(SELENIUM_PAGE_KIND_META_KEY, "detail")
        try:
            with self.browser.lock:
                try:
                    if page_kind == "search":
                        response = self._render_search(request, spider)
                    else:
                        response = self._render_detail(request, spider)
                finally:
                    self._pause_after_first_request(spider, page_kind)
                return response
        except CloseSpider:
            raise
        except (TimeoutException, WebDriverException) as error:
            artifact = self._save_artifact(page_kind)
            spider.logger.error(
                "Selenium failed for %s (%s). Diagnostic artifact: %s",
                request.url,
                error,
                artifact,
            )
            raise

    def _pause_after_first_request(self, spider, page_kind: str) -> None:
        seconds = self.debug_pause_after_first_request_seconds
        if (
            seconds <= 0
            or self._first_request_debug_pause_done
            or self.browser.launch_options.headless
            or not self.browser.ready
        ):
            return
        self._first_request_debug_pause_done = True
        artifact = self._save_artifact(f"debug-first-{page_kind}")
        current_url = self.browser.driver.current_url if self.browser.driver else ""
        spider.logger.warning(
            "DEBUG PAUSE: first Selenium %s navigation loaded at %s. "
            "Browser remains open for %.0f seconds; artifact: %s",
            page_kind,
            current_url,
            seconds,
            artifact,
        )
        # This code already runs in deferToThread, so sleeping here keeps the
        # browser and request alive without blocking Twisted's reactor thread.
        time.sleep(seconds)

    def _render_search(self, request, spider):
        session_key = request.meta.get("search_session_key") or request.url
        continue_scroll = bool(request.meta.get("continue_scroll"))
        state = self.search_states.setdefault(session_key, SearchScrollState())

        driver = self.browser.ensure_session()
        if not continue_scroll or "/jobs/search" not in (driver.current_url or ""):
            self.browser.open_url(request.url)
            # Pause before dismissing sign-in UI or evaluating auth/challenge
            # markers so the developer sees and can interact with the raw page.
            self._pause_after_first_request(spider, "search")
            self._wait_for_search_page(driver)
            self._dismiss_sign_in_modal(driver)
            if self._is_empty_search_page(driver):
                state.exhausted = True
                state.stop_reason = "empty_search"

        self._raise_if_blocked("search")

        target_total = int(request.meta.get("target_total", 500))
        chunk_size = int(request.meta.get("chunk_size", 50))
        max_scroll_rounds = int(request.meta.get("max_scroll_rounds", self.max_scroll_rounds))
        if not 1 <= max_scroll_rounds <= 5000:
            raise ValueError("max_scroll_rounds must be between 1 and 5000")
        target_this_call = min(target_total, len(state.returned_ids) + chunk_size)

        while len(state.jobs) < target_this_call and not state.exhausted:
            self._collect_search_jobs(driver, state)
            if len(state.jobs) >= target_this_call:
                break

            if state.scrolls_completed >= max_scroll_rounds:
                state.exhausted = True
                state.stop_reason = "max_scroll_rounds"
                break
            if time.monotonic() - state.started_monotonic >= self.search_deadline_seconds:
                artifact = self._save_artifact("search-deadline")
                raise CloseSpider(
                    "linkedin_search_deadline: search did not reach max_results or "
                    f"max_scroll_rounds; diagnostic saved to {artifact}"
                )

            viewed_all = self._viewed_all_jobs(driver)
            if viewed_all and not state.viewed_all_observed:
                state.viewed_all_observed = True
                spider.logger.warning(
                    "LinkedIn displayed viewed-all after %s scrolls, but the configured "
                    "search continues until max_results=%s or max_scroll_rounds=%s",
                    state.scrolls_completed,
                    target_total,
                    max_scroll_rounds,
                )

            before = len(state.jobs)
            before_height = self._search_page_height(driver)
            show_more_clicked = self._click_show_more(driver)
            driver.execute_script(
                "window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'});"
            )
            state.scrolls_completed += 1
            wait_outcome = self._wait_for_search_growth(
                driver,
                state,
                before_unique=before,
                before_height=before_height,
            )

            if len(state.jobs) == before:
                state.stagnation += 1
            else:
                state.stagnation = 0
            if state.stagnation == self.stagnation_limit:
                spider.logger.warning(
                    "LinkedIn search had %s consecutive scrolls without new IDs; "
                    "continuing because max_scroll_rounds=%s is authoritative",
                    state.stagnation,
                    max_scroll_rounds,
                )
            spider.logger.info(
                "LinkedIn search scroll %s/%s: discovered=%s (+%s), "
                "show_more=%s, viewed_all=%s, wait=%s",
                state.scrolls_completed,
                max_scroll_rounds,
                len(state.jobs),
                len(state.jobs) - before,
                show_more_clicked,
                viewed_all,
                wait_outcome,
            )

        new_jobs = [job for job_id, job in state.jobs.items() if job_id not in state.returned_ids]
        remaining = max(0, target_total - len(state.returned_ids))
        new_jobs = new_jobs[: min(chunk_size, remaining)]
        for job in new_jobs:
            identity = self._job_identity(job)
            if identity:
                state.returned_ids.add(identity)

        if len(state.jobs) >= target_total:
            state.stop_reason = "max_results"
        elif state.scrolls_completed >= max_scroll_rounds:
            state.stop_reason = "max_scroll_rounds"
        discovery_done = state.stop_reason is not None
        state.exhausted = discovery_done
        deliverable_total = min(len(state.jobs), target_total)
        buffered_remaining = len(state.returned_ids) < deliverable_total
        can_continue = buffered_remaining or not discovery_done
        payload = {
            "jobs": new_jobs,
            "total_unique": len(state.jobs),
            "returned_total": len(state.returned_ids),
            "scrolls_completed": state.scrolls_completed,
            "max_scroll_rounds": max_scroll_rounds,
            "discovery_done": discovery_done,
            "stop_reason": state.stop_reason,
            "exhausted": not can_continue,
            "can_continue": can_continue,
            "current_url": driver.current_url,
        }
        self.crawler.stats.set_value(
            "job_harvester/search_scrolls_completed", state.scrolls_completed
        )
        self.crawler.stats.set_value(
            "job_harvester/search_discovery_stop_reason", state.stop_reason
        )
        spider.logger.info(
            "LinkedIn search chunk: returned=%s discovered=%s target=%s "
            "scrolls=%s/%s exhausted=%s stop_reason=%s",
            len(new_jobs),
            len(state.jobs),
            target_total,
            state.scrolls_completed,
            max_scroll_rounds,
            payload["exhausted"],
            payload["stop_reason"],
        )
        return TextResponse(
            url=driver.current_url,
            status=200,
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            encoding="utf-8",
            request=request,
        )

    def _collect_search_jobs(self, driver, state: SearchScrollState) -> int:
        before = len(state.jobs)
        for job in self._extract_search_cards(driver):
            job_id = self._job_identity(job)
            if not job_id:
                continue
            existing = state.jobs.get(job_id)
            if existing is None:
                state.jobs[job_id] = job
                continue
            for key, value in job.items():
                if value and not existing.get(key):
                    existing[key] = value
        return len(state.jobs) - before

    @staticmethod
    def _search_page_height(driver) -> int:
        return int(
            driver.execute_script(
                "return Math.max(document.body.scrollHeight, "
                "document.documentElement.scrollHeight);"
            )
            or 0
        )

    def _wait_for_search_growth(
        self,
        driver,
        state: SearchScrollState,
        *,
        before_unique: int,
        before_height: int,
    ) -> str:
        started = time.monotonic()
        minimum_pause = random.uniform(self.pause_min, self.pause_max)
        deadline = started + max(6.0, self.pause_max)
        last_state: tuple[int, int] | None = None
        stable_polls = 0
        growth_observed = False

        while time.monotonic() < deadline:
            time.sleep(0.5)
            self._dismiss_sign_in_modal(driver)
            self._raise_if_blocked("search")
            self._collect_search_jobs(driver, state)
            current_height = self._search_page_height(driver)
            current_state = (len(state.jobs), current_height)
            if len(state.jobs) > before_unique or current_height > before_height:
                growth_observed = True
            if current_state == last_state:
                stable_polls += 1
            else:
                last_state = current_state
                stable_polls = 0
            if (
                time.monotonic() - started >= minimum_pause
                and growth_observed
                and stable_polls >= 2
            ):
                return "growth-settled"
        return "timeout-no-growth"

    def _render_detail(self, request, spider):
        driver = self.browser.ensure_session()
        self.browser.open_url(request.url)
        self._pause_after_first_request(spider, "detail")
        self._wait_for_detail_page(driver)
        self._dismiss_sign_in_modal(driver)
        self._raise_if_blocked("detail")
        description_loaded = self._wait_for_full_job_description(driver)
        request.meta["linkedin_description_loaded"] = description_loaded
        self._raise_if_blocked("detail")
        if not description_loaded:
            artifact = self._save_artifact("missing-description")
            spider.logger.warning(
                "LinkedIn detail shell loaded, but the full description did not stabilize. "
                "Saving a partial snapshot; diagnostic artifact: %s",
                artifact,
            )
        status = self._navigation_status(driver)
        if status in {429, 999}:
            artifact = self._save_artifact(f"rate-limit-{status}")
            raise CloseSpider(f"linkedin_rate_limit_{status}: diagnostic saved to {artifact}")
        return HtmlResponse(
            url=driver.current_url,
            status=status,
            body=driver.page_source.encode("utf-8"),
            encoding="utf-8",
            request=request,
        )

    def _wait_for_search_page(self, driver):
        WebDriverWait(driver, self.wait_timeout).until(
            lambda current: (
                current.find_elements(
                    By.CSS_SELECTOR,
                    "[data-entity-urn^='urn:li:jobPosting:'], a[href*='/jobs/view/']",
                )
                or self._is_empty_search_page(current)
                or self._is_blocked_page(current)
            )
        )

    @staticmethod
    def _is_empty_search_page(driver) -> bool:
        if driver.execute_script("return document.readyState") != "complete":
            return False
        empty_selectors = (
            ".jobs-search-no-results-banner",
            ".no-results",
            ".jobs-search-results-list__empty-state",
        )
        if any(driver.find_elements(By.CSS_SELECTOR, item) for item in empty_selectors):
            return True
        try:
            body = (driver.find_element(By.TAG_NAME, "body").text or "").casefold()
        except WebDriverException:
            return False
        markers = (
            "no matching jobs found",
            "no jobs found",
            "0 вакансий",
            "подходящих вакансий не найдено",
        )
        return any(marker in body for marker in markers)

    def _wait_for_detail_page(self, driver):
        WebDriverWait(driver, self.wait_timeout).until(
            lambda current: (
                current.find_elements(
                    By.CSS_SELECTOR,
                    "script[type='application/ld+json'], "
                    "[id^='JobDetails_AboutTheJob_'], "
                    ".show-more-less-html__markup, "
                    ".jobs-description-content__text, "
                    ".jobs-description__content, "
                    "#job-details",
                )
                or self._is_blocked_page(current)
            )
        )

    def _wait_for_full_job_description(self, driver) -> bool:
        """Wait until description HTML is non-empty and stable across two polls."""

        deadline = time.monotonic() + self.wait_timeout
        last_snapshot: str | None = None
        stable_polls = 0
        dom_first_seen_at: float | None = None
        scrolled_without_container = False

        while time.monotonic() < deadline:
            if self._is_blocked_page(driver):
                return False

            snapshot = self._description_snapshot(driver)
            if not (snapshot or "").startswith("jsonld:"):
                container = self._find_description_container(driver)
                if container is not None:
                    with suppress(WebDriverException):
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});",
                            container,
                        )
                    if self._expand_job_description(driver, container):
                        last_snapshot = None
                        stable_polls = 0
                        dom_first_seen_at = time.monotonic()
                        time.sleep(0.5)
                        continue
                elif not scrolled_without_container:
                    scrolled_without_container = True
                    with suppress(WebDriverException):
                        driver.execute_script(
                            "window.scrollTo({top: document.body.scrollHeight * 0.6, "
                            "behavior: 'instant'});"
                        )
                snapshot = self._description_snapshot(driver)

            if snapshot:
                if snapshot.startswith("dom:"):
                    dom_first_seen_at = dom_first_seen_at or time.monotonic()
                else:
                    dom_first_seen_at = None
                if snapshot == last_snapshot:
                    stable_polls += 1
                else:
                    last_snapshot = snapshot
                    stable_polls = 1
                dom_has_settled = (
                    dom_first_seen_at is None
                    or time.monotonic() - dom_first_seen_at >= DOM_DESCRIPTION_SETTLE_SECONDS
                )
                if stable_polls >= 2 and dom_has_settled:
                    return True
            else:
                last_snapshot = None
                stable_polls = 0
                dom_first_seen_at = None
            time.sleep(0.5)
        return False

    @staticmethod
    def _find_description_container(driver):
        for selector in DETAIL_DESCRIPTION_SELECTORS:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                return elements[0]
        return None

    @staticmethod
    def _expand_job_description(driver, container) -> bool:
        """Expand a truncated description, including sibling controls."""

        markers = (
            "show more",
            "see more",
            "read more",
            "показать ещё",
            "показать больше",
            "daha fazla göster",
        )
        candidates = []
        with suppress(WebDriverException):
            scope = container
            for _ in range(3):
                candidates.extend(scope.find_elements(By.CSS_SELECTOR, "button"))
                scope = scope.find_element(By.XPATH, "..")
        for selector in DESCRIPTION_EXPAND_SELECTORS:
            with suppress(WebDriverException):
                candidates.extend(driver.find_elements(By.CSS_SELECTOR, selector))

        seen: set[str] = set()
        for button in candidates:
            with suppress(WebDriverException):
                element_id = button.id
                if element_id in seen:
                    continue
                seen.add(element_id)
                label = " ".join(
                    filter(
                        None,
                        (
                            button.get_attribute("aria-label"),
                            button.get_attribute("title"),
                            button.get_attribute("textContent"),
                        ),
                    )
                ).casefold()
                if (
                    any(marker in label for marker in markers)
                    and button.is_displayed()
                    and button.is_enabled()
                ):
                    driver.execute_script("arguments[0].click();", button)
                    return True
        return False

    @staticmethod
    def _description_snapshot(driver) -> str | None:
        structured_description = ""
        with suppress(WebDriverException):
            structured_description = (
                driver.execute_script(
                    r"""
                const visit = (value) => {
                  if (Array.isArray(value)) {
                    for (const item of value) {
                      const found = visit(item);
                      if (found) return found;
                    }
                    return '';
                  }
                  if (!value || typeof value !== 'object') return '';
                  const types = Array.isArray(value['@type'])
                    ? value['@type'] : [value['@type']];
                  if (types.includes('JobPosting') && typeof value.description === 'string') {
                    return value.description.trim();
                  }
                  if (value['@graph']) {
                    const graphValue = visit(value['@graph']);
                    if (graphValue) return graphValue;
                  }
                  return '';
                };
                for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
                  try {
                    const found = visit(JSON.parse(node.textContent || ''));
                    if (found) return found;
                  } catch (_) {}
                }
                return '';
                """
                )
                or ""
            )
        if len(" ".join(structured_description.split())) >= 80:
            return f"jsonld:{structured_description}"

        candidates: list[tuple[int, str]] = []
        for selector in DETAIL_DESCRIPTION_SELECTORS:
            with suppress(WebDriverException):
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    text = " ".join((element.get_attribute("textContent") or "").split())
                    if len(text) < 80:
                        continue
                    inner_html = element.get_attribute("innerHTML") or text
                    candidates.append((len(text), f"dom:{inner_html}"))
        return max(candidates, default=(0, None), key=lambda item: item[0])[1]

    @staticmethod
    def _extract_search_cards(driver) -> list[dict]:
        script = r"""
            const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
            const text = (root, selectors) => {
              for (const selector of selectors) {
                const node = root.querySelector(selector);
                if (node && clean(node.textContent)) return clean(node.textContent);
              }
              return '';
            };
            const nodes = Array.from(document.querySelectorAll(
              '[data-entity-urn^="urn:li:jobPosting:"]'
            ));
            const fallback = nodes.length ? [] : Array.from(document.querySelectorAll(
              'a[href*="/jobs/view/"]'
            ));
            return nodes.concat(fallback).map((node) => {
              const card = node.closest('li')?.querySelector(
                '[data-entity-urn^="urn:li:jobPosting:"]'
              ) || node.closest('li, .job-search-card, .base-card') || node;
              const anchor = card.matches('a[href]') ? card : card.querySelector(
                'a.base-card__full-link[href], '
                + 'a[data-tracking-control-name="public_jobs_jserp-result_search-card"][href], '
                + 'a[href*="/jobs/view/"]'
              );
              const time = card.querySelector('time');
              return {
                entity_urn: card.getAttribute('data-entity-urn') || '',
                href: anchor ? anchor.href : '',
                title: text(card, [
                  '.base-search-card__title', '.job-card-list__title--link', 'h3',
                  '.base-card__full-link .sr-only'
                ]),
                company: text(card, [
                  '.base-search-card__subtitle',
                  '.job-card-container__primary-description', 'h4'
                ]),
                location: text(card, [
                  '.job-search-card__location', '.artdeco-entity-lockup__caption'
                ]),
                posted_at: time ? (time.getAttribute('datetime') || clean(time.textContent)) : '',
                card_text: clean(card.innerText || card.textContent),
              };
            }).filter((item) => item.href || item.entity_urn);
        """
        return driver.execute_script(script) or []

    @staticmethod
    def _job_identity(job: dict) -> str | None:
        urn_match = JOB_ID_FROM_URN.search(job.get("entity_urn") or "")
        if urn_match:
            job_id = urn_match.group(1)
            job["source_job_id"] = job_id
            if not job.get("href"):
                job["href"] = f"https://www.linkedin.com/jobs/view/{job_id}/"
            return job_id
        url_match = JOB_ID_FROM_URL.search(job.get("href") or "")
        if url_match:
            job["source_job_id"] = url_match.group(1)
            return url_match.group(1)
        return job.get("href") or None

    @staticmethod
    def _click_show_more(driver) -> bool:
        selectors = (
            "button.infinite-scroller__show-more-button--visible",
            "button[data-tracking-control-name='infinite-scroller_show-more']",
        )
        for selector in selectors:
            for button in driver.find_elements(By.CSS_SELECTOR, selector):
                if not button.is_displayed() or not button.is_enabled():
                    continue
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
                driver.execute_script("arguments[0].click();", button)
                return True
        return False

    @staticmethod
    def _viewed_all_jobs(driver) -> bool:
        for node in driver.find_elements(By.CSS_SELECTOR, ".see-more-jobs__viewed-all"):
            classes = node.get_attribute("class") or ""
            if node.is_displayed() and "hidden" not in classes.split():
                return True
        return False

    @staticmethod
    def _dismiss_sign_in_modal(driver):
        selectors = (
            ".contextual-sign-in-modal .modal__dismiss",
            "button.modal__dismiss",
            "button[aria-label='Dismiss']",
        )
        for selector in selectors:
            for button in driver.find_elements(By.CSS_SELECTOR, selector):
                if button.is_displayed():
                    try:
                        driver.execute_script("arguments[0].click();", button)
                        return
                    except WebDriverException:
                        continue

    def _raise_if_blocked(self, page_kind: str):
        driver = self.browser.driver
        if driver is None or not self._is_blocked_page(driver):
            return
        artifact = self._save_artifact(f"blocked-{page_kind}")
        raise CloseSpider(
            "linkedin_auth_or_challenge: LinkedIn requested login/security verification; "
            f"diagnostic saved to {artifact}"
        )

    @staticmethod
    def _is_blocked_page(driver) -> bool:
        url = (driver.current_url or "").lower()
        if any(
            marker in url
            for marker in (
                "/checkpoint/",
                "/challenge/",
                "/uas/login",
                "linkedin.com/login",
                "/authwall",
            )
        ):
            return True
        title = (driver.title or "").casefold()
        body = ""
        with suppress(WebDriverException):
            body = (driver.find_element(By.TAG_NAME, "body").text or "")[:4000].casefold()
        markers = (
            "security verification",
            "let's do a quick security check",
            "unusual activity",
            "verify you are a human",
            "too many requests",
            "temporarily restricted",
            "проверка безопасности",
        )
        return any(marker in title or marker in body for marker in markers)

    @staticmethod
    def _navigation_status(driver) -> int:
        try:
            status = driver.execute_script(
                "const e=performance.getEntriesByType('navigation');"
                "return e.length && e[e.length-1].responseStatus || 200;"
            )
            return int(status or 200)
        except (TypeError, ValueError, WebDriverException):
            return 200

    def _save_artifact(self, label: str) -> str:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = self.artifact_dir / f"{label}-{stamp}"
        driver = self.browser.driver
        if driver is not None:
            with suppress(OSError, WebDriverException):
                base.with_suffix(".html").write_text(driver.page_source, encoding="utf-8")
            with suppress(OSError, WebDriverException):
                driver.save_screenshot(str(base.with_suffix(".png")))
        return str(base)

    def spider_closed(self, spider, reason):
        self.browser.close()
        self.search_states.clear()
