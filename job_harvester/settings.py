from __future__ import annotations

import os
from pathlib import Path

RUNTIME_ROOT = Path.cwd()

BOT_NAME = "job_harvester"
SPIDER_MODULES = ["job_harvester.spiders"]
NEWSPIDER_MODULE = "job_harvester.spiders"

# Selenium owns navigation and cookies for marked requests.
ROBOTSTXT_OBEY = False
COOKIES_ENABLED = False
CONCURRENT_REQUESTS = 1
DOWNLOAD_DELAY = 0
DOWNLOAD_TIMEOUT = 60
RETRY_ENABLED = True
RETRY_TIMES = 1

DOWNLOADER_MIDDLEWARES = {
    "job_harvester.middlewares.SeleniumBaseMiddleware": 543,
}
ITEM_PIPELINES = {
    "job_harvester.pipelines.SQLiteVacancyPipeline": 300,
}

JOB_DATABASE = os.getenv("JOB_HARVESTER_DB", str(RUNTIME_ROOT / "data" / "jobs.sqlite3"))
JOB_DATABASE_COMMIT_EVERY = int(os.getenv("JOB_HARVESTER_COMMIT_EVERY", "20"))

SELENIUM_BROWSER_HEADLESS = os.getenv("LINKEDIN_HEADLESS", "0") == "1"
SELENIUM_BROWSER_INCOGNITO = os.getenv("LINKEDIN_INCOGNITO", "0") == "1"
SELENIUM_BROWSER_USE_UC = os.getenv("LINKEDIN_USE_UC", "0") == "1"
SELENIUM_BROWSER_PROFILE_DIR = os.getenv(
    "LINKEDIN_PROFILE_DIR", str(RUNTIME_ROOT / ".browser-profile")
)
SELENIUM_BROWSER_LOCALE_CODE = os.getenv("LINKEDIN_LOCALE", "en-US")
SELENIUM_BROWSER_WINDOW_SIZE = os.getenv("LINKEDIN_WINDOW_SIZE", "1440,1000")
SELENIUM_BROWSER_PAGE_LOAD_TIMEOUT = int(os.getenv("LINKEDIN_PAGE_TIMEOUT", "45"))
SELENIUM_BROWSER_SCRIPT_TIMEOUT = int(os.getenv("LINKEDIN_SCRIPT_TIMEOUT", "30"))
SELENIUM_BROWSER_WAIT_TIMEOUT = int(os.getenv("LINKEDIN_WAIT_TIMEOUT", "20"))
SELENIUM_BROWSER_SCROLL_PAUSE_MIN = float(os.getenv("LINKEDIN_SCROLL_PAUSE_MIN", "1.0"))
SELENIUM_BROWSER_SCROLL_PAUSE_MAX = float(os.getenv("LINKEDIN_SCROLL_PAUSE_MAX", "2.0"))
SELENIUM_BROWSER_STAGNATION_LIMIT = int(os.getenv("LINKEDIN_STAGNATION_LIMIT", "3"))
SELENIUM_BROWSER_MAX_SCROLL_ROUNDS = int(os.getenv("LINKEDIN_MAX_SCROLL_ROUNDS", "250"))
SELENIUM_BROWSER_SEARCH_DEADLINE_SECONDS = int(os.getenv("LINKEDIN_SEARCH_DEADLINE_SECONDS", "900"))
SELENIUM_BROWSER_DEBUG_PAUSE_AFTER_FIRST_REQUEST_SECONDS = float(
    os.getenv("LINKEDIN_DEBUG_PAUSE_AFTER_FIRST_REQUEST_SECONDS", "0")
)
SELENIUM_BROWSER_ARTIFACT_DIR = os.getenv("LINKEDIN_ARTIFACT_DIR", str(RUNTIME_ROOT / ".artifacts"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
FEED_EXPORT_ENCODING = "utf-8"
TELNETCONSOLE_ENABLED = False

# Scrapy 2.13's supported asyncio reactor; Selenium work is delegated to a thread.
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
