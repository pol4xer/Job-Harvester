"""Scrapy item pipeline backed by the append-only SQLite storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .storage import SQLiteStorage, item_to_mapping

try:  # Keeps the persistence module importable before Scrapy is installed.
    from scrapy.exceptions import DropItem
except ImportError:  # pragma: no cover - exercised only in minimal environments

    class DropItem(Exception):
        """Fallback matching Scrapy's invalid-item exception."""


class SQLiteJobPipeline:
    """Persist jobs, immutable snapshots, search runs, and query hits in batches."""

    def __init__(
        self,
        database_path: str | Path = "data/jobs.sqlite3",
        *,
        batch_size: int = 50,
        busy_timeout_ms: int = 10_000,
        crawler: Any = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self.batch_size = max(int(batch_size), 1)
        self.busy_timeout_ms = max(int(busy_timeout_ms), 1)
        self.crawler = crawler
        self.storage: SQLiteStorage | None = None
        self.run_id: int | None = None
        self._pending = 0
        self._counters = {
            "processed": 0,
            "new_jobs": 0,
            "new_snapshots": 0,
            "duplicate_snapshots": 0,
            "new_hits": 0,
            "errors": 0,
            "commits": 0,
        }

    @classmethod
    def from_crawler(cls, crawler: Any) -> SQLiteJobPipeline:
        settings = crawler.settings
        database_path = (
            settings.get("JOB_DATABASE")
            or settings.get("JOB_HARVESTER_DATABASE")
            or settings.get("JOB_HARVESTER_DB_PATH")
            or settings.get("SQLITE_DATABASE")
            or "data/jobs.sqlite3"
        )
        batch_size = _setting_int(
            settings,
            "JOB_DATABASE_COMMIT_EVERY",
            "JOB_HARVESTER_COMMIT_BATCH_SIZE",
            "SQLITE_COMMIT_BATCH_SIZE",
            default=50,
        )
        busy_timeout_ms = _setting_int(
            settings,
            "JOB_HARVESTER_BUSY_TIMEOUT_MS",
            "SQLITE_BUSY_TIMEOUT_MS",
            default=10_000,
        )
        pipeline = cls(
            database_path,
            batch_size=batch_size,
            busy_timeout_ms=busy_timeout_ms,
            crawler=crawler,
        )
        from scrapy import signals

        crawler.signals.connect(pipeline.spider_closed, signal=signals.spider_closed)
        return pipeline

    def open_spider(self) -> None:
        spider = self.crawler.spider
        self.storage = SQLiteStorage(
            self.database_path,
            busy_timeout_ms=self.busy_timeout_ms,
        )
        run_config = _spider_value(
            spider,
            "run_metadata",
            "run_config",
            "search_config",
            "config",
        )
        if run_config is None:
            run_config = {
                "searches": _spider_value(spider, "searches", "search_profiles"),
                "headless": _spider_value(spider, "headless"),
            }
            run_config = {key: value for key, value in run_config.items() if value is not None}
        self.run_id = self.storage.start_search_run(
            source=_spider_value(spider, "source") or "other",
            spider_name=_spider_value(spider, "name") or type(spider).__name__,
            search_profile_id=_spider_value(spider, "search_profile_id", "profile_id", "search_id"),
            query=_spider_value(spider, "query", "keywords"),
            location=_spider_value(spider, "location", "search_location"),
            search_url=_spider_value(spider, "search_url", "start_url"),
            requested_limit=_spider_value(
                spider, "max_jobs", "max_results", "requested_limit", "limit"
            ),
            config=run_config,
            run_uuid=_spider_value(spider, "run_id"),
        )
        self.storage.commit()
        self._set_stat("job_harvester/run_id", self.run_id)

    def process_item(self, item: Any) -> Any:
        if self.storage is None or self.run_id is None:
            self.open_spider()
        assert self.storage is not None
        assert self.run_id is not None

        try:
            # Conversion happens here as well as in storage so invalid custom items
            # are counted consistently before becoming a Scrapy DropItem.
            record = item_to_mapping(item)
            source = _spider_value(self.crawler.spider, "source")
            if source is not None and record.get("source") != source:
                raise ValueError("Vacancy source does not match the active adapter")
            result = self.storage.store_job(item, run_id=self.run_id)
        except (TypeError, ValueError) as exc:
            self._record_error()
            raise DropItem(str(exc)) from exc
        except sqlite3.Error:
            self._record_error()
            raise

        self._pending += 1
        self._counters["processed"] += 1
        self._counters["new_jobs"] += int(result.new_job)
        self._counters["new_snapshots"] += int(result.new_snapshot)
        self._counters["duplicate_snapshots"] += int(not result.new_snapshot)
        self._counters["new_hits"] += int(result.new_hit)

        self._inc_stat("job_harvester/items_processed")
        self._inc_stat("job_harvester/jobs_new", int(result.new_job))
        self._inc_stat("job_harvester/snapshots_new", int(result.new_snapshot))
        self._inc_stat("job_harvester/snapshots_duplicate", int(not result.new_snapshot))
        self._inc_stat("job_harvester/search_hits_new", int(result.new_hit))

        if isinstance(item, dict):
            item["dedup_key"] = result.dedup_key
            item["content_hash"] = result.content_hash

        if self._pending >= self.batch_size:
            self._commit()
        return item

    def close_spider(self) -> None:
        # Scrapy calls this hook before it emits the real closing reason.
        # Flush here; finalize metadata and release SQLite in spider_closed.
        self._commit(force=True)

    def spider_closed(self, spider: Any, reason: str) -> None:
        if self.storage is None:
            return
        reason = _spider_value(spider, "close_reason", "finish_reason") or reason
        errors = self._counters["errors"]
        if self.crawler is not None:
            errors += self.crawler.stats.get_value("job_harvester/source_errors", 0)
            errors += self.crawler.stats.get_value("log_count/ERROR", 0)
        status = "failed" if errors else "finished" if reason == "finished" else "stopped"
        try:
            if self.run_id is not None:
                self.storage.finish_search_run(self.run_id, status=status, close_reason=reason)
            self._commit(force=True)
        finally:
            self.storage.close()
            self.storage = None
        for name, value in self._counters.items():
            self._set_stat(f"job_harvester/final/{name}", value)

    def _record_error(self) -> None:
        self._counters["errors"] += 1
        self._inc_stat("job_harvester/errors")
        if self.storage is not None and self.run_id is not None:
            self.storage.record_error(self.run_id)
            self._pending += 1
            if self._pending >= self.batch_size:
                self._commit()

    def _commit(self, *, force: bool = False) -> None:
        if self.storage is None or (not force and self._pending == 0):
            return
        self.storage.commit()
        self._pending = 0
        self._counters["commits"] += 1
        self._inc_stat("job_harvester/sqlite_commits")

    def _inc_stat(self, key: str, count: int = 1) -> None:
        if count and self.crawler is not None:
            self.crawler.stats.inc_value(key, count=count)

    def _set_stat(self, key: str, value: Any) -> None:
        if self.crawler is not None:
            self.crawler.stats.set_value(key, value)


def _setting_int(settings: Any, *names: str, default: int) -> int:
    for name in names:
        value = settings.get(name)
        if value is not None:
            return int(value)
    return default


def _spider_value(spider: Any, *names: str) -> Any:
    for name in names:
        value = getattr(spider, name, None)
        if value is not None and value != "":
            return value
    return None


class SQLiteVacancyPipeline(SQLiteJobPipeline):
    """Project-facing name retained by ``job_harvester.settings``."""


__all__ = ["SQLiteJobPipeline", "SQLiteVacancyPipeline"]
