from __future__ import annotations

import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class BrowserLaunchOptions:
    """Launch options for a reusable SeleniumBase browser session."""

    browser: str = "chrome"
    headless: bool = False
    incognito: bool = False
    use_uc: bool = False
    profile_dir: str | None = None
    locale_code: str = "en-US"
    window_size: str = "1440,1000"
    page_load_timeout: int = 45
    script_timeout: int = 30
    chromium_args: tuple[str, ...] = (
        "--disable-dev-shm-usage",
        "--disable-notifications",
    )

    @classmethod
    def from_settings(cls, settings) -> BrowserLaunchOptions:
        chromium_args = settings.getlist("SELENIUM_BROWSER_CHROMIUM_ARGS")
        if not chromium_args:
            chromium_args = ["--disable-dev-shm-usage", "--disable-notifications"]
        profile_dir = settings.get("SELENIUM_BROWSER_PROFILE_DIR") or None
        if profile_dir:
            profile_dir = str(Path(profile_dir).expanduser().resolve())
        return cls(
            browser=settings.get("SELENIUM_BROWSER_BROWSER", "chrome"),
            headless=settings.getbool("SELENIUM_BROWSER_HEADLESS", False),
            incognito=settings.getbool("SELENIUM_BROWSER_INCOGNITO", False),
            use_uc=settings.getbool("SELENIUM_BROWSER_USE_UC", False),
            profile_dir=profile_dir,
            locale_code=settings.get("SELENIUM_BROWSER_LOCALE_CODE", "en-US"),
            window_size=settings.get("SELENIUM_BROWSER_WINDOW_SIZE", "1440,1000"),
            page_load_timeout=settings.getint("SELENIUM_BROWSER_PAGE_LOAD_TIMEOUT", 45),
            script_timeout=settings.getint("SELENIUM_BROWSER_SCRIPT_TIMEOUT", 30),
            chromium_args=tuple(chromium_args),
        )

    def to_seleniumbase_options(self) -> dict:
        options = {
            "browser": self.browser,
            "headless": self.headless,
            "headed": not self.headless,
            "uc": self.use_uc,
            "undetectable": self.use_uc,
            "incognito": self.incognito,
            "locale_code": self.locale_code,
            "window_size": self.window_size,
            "chromium_arg": ",".join(self.chromium_args),
        }
        # Incognito debugging must not silently reuse cookies from the
        # persistent Selenium profile.
        if self.profile_dir and not self.incognito:
            Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
            options["user_data_dir"] = self.profile_dir
        return options


class SeleniumBaseSessionProvider:
    """Owns one long-lived SeleniumBase context for a Scrapy spider run."""

    def __init__(self, launch_options: BrowserLaunchOptions):
        self.launch_options = launch_options
        self.driver = None
        self.sb = None
        self.sb_context = None
        self.lock = threading.RLock()

    @property
    def ready(self) -> bool:
        return self.driver is not None

    def ensure_session(self):
        with self.lock:
            if self.driver is not None:
                return self.driver
            from seleniumbase import SB

            try:
                self.sb_context = SB(**self.launch_options.to_seleniumbase_options())
                self.sb = self.sb_context.__enter__()
                self.driver = self.sb.driver
                self.driver.set_page_load_timeout(self.launch_options.page_load_timeout)
                self.driver.set_script_timeout(self.launch_options.script_timeout)
                return self.driver
            except Exception:
                self.close()
                raise

    def open_url(self, url: str):
        driver = self.ensure_session()
        if self.launch_options.use_uc and hasattr(driver, "uc_open_with_reconnect"):
            driver.uc_open_with_reconnect(url, reconnect_time=5)
        elif self.launch_options.use_uc and hasattr(driver, "uc_open"):
            driver.uc_open(url)
        else:
            driver.get(url)

    def close(self):
        with self.lock:
            driver = self.driver
            context = self.sb_context
            self.driver = None
            self.sb = None
            self.sb_context = None

            if context is not None:
                with suppress(Exception):
                    context.__exit__(None, None, None)
            if driver is not None:
                with suppress(Exception):
                    driver.quit()

    def __enter__(self) -> SeleniumBaseSessionProvider:
        self.ensure_session()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
