"""A real second adapter using local records instead of a website."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import scrapy

from job_harvester.items import Vacancy


class JsonlJobsSpider(scrapy.Spider):
    name = "jsonl_jobs"
    source = "jsonl"
    custom_settings = {
        "DOWNLOADER_MIDDLEWARES": {"job_harvester.middlewares.SeleniumBaseMiddleware": None},
        "DOWNLOAD_HANDLERS": {"http": None, "https": None, "ftp": None, "file": None},
    }

    def __init__(self, input_path: str, max_results: int | None = None, **kwargs):
        super().__init__(**kwargs)
        self.input_path = Path(input_path).expanduser().resolve()
        if not self.input_path.is_file():
            raise ValueError("JSONL input must be an existing local file")
        self.max_results = int(max_results) if max_results is not None else None
        if self.max_results is not None and self.max_results < 1:
            raise ValueError("max_results must be positive")
        self.run_id = str(uuid4())
        self.profile_id = "local_import"
        self.run_metadata = {"source": self.source, "format": "jsonl"}

    async def start(self):
        rank = 0
        # No Request objects are produced: even source_url/apply_url are data.
        with self.input_path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise ValueError("Each record must be a JSON object")
                    # Adapter ownership prevents an input record impersonating
                    # another source or carrying a stale identity/content hash.
                    payload = {
                        key: value
                        for key, value in payload.items()
                        if key not in {"dedup_key", "content_hash"}
                    }
                    payload.update(
                        source=self.source,
                        search_run_id=self.run_id,
                        search_profile_id=self.profile_id,
                        search_rank=rank + 1,
                        captured_at=datetime.now(UTC),
                    )
                    vacancy = Vacancy.model_validate(payload)
                except (ValueError, TypeError):
                    self.close_reason = "invalid_input"
                    self.crawler.stats.inc_value("job_harvester/source_errors")
                    # Do not echo potentially sensitive imported field values.
                    raise ValueError(f"Invalid JSONL vacancy on line {line_number}") from None
                rank += 1
                yield vacancy.as_dict()
                if self.max_results is not None and rank >= self.max_results:
                    break
