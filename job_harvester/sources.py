"""Explicit, lazy source registration; storage and exporters know no adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import import_module
from typing import Any

SOURCE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    slug: str
    description: str
    spider_path: str
    requires_browser: bool = False
    requires_input: bool = False

    def load_spider(self) -> Any:
        from scrapy import Spider

        module, _, name = self.spider_path.rpartition(".")
        spider = getattr(import_module(module), name)
        if getattr(spider, "source", None) != self.slug:
            raise ValueError(f"Source {self.slug!r} must match its spider's source attribute")
        if not isinstance(spider, type) or not issubclass(spider, Spider):
            raise ValueError("Registered adapters must be Scrapy Spider classes")
        return spider


_SOURCES: dict[str, SourceDefinition] = {}


def register_source(definition: SourceDefinition) -> None:
    """Register an implemented adapter before calling ``run_crawl``.

    Each spider declares the same ``source`` slug and yields ``Vacancy``
    dictionaries. Reusing the shared pipeline requires no database migrations
    or exporter edits. Registration deliberately rejects accidental overrides.
    """
    if not SOURCE_SLUG.fullmatch(definition.slug):
        raise ValueError("Source slug must contain lowercase letters, digits, '_' or '-'")
    if definition.slug in _SOURCES:
        raise ValueError(f"Source {definition.slug!r} is already registered")
    if "." not in definition.spider_path:
        raise ValueError("spider_path must be a dotted Python import path")
    _SOURCES[definition.slug] = definition


def list_sources() -> tuple[SourceDefinition, ...]:
    return tuple(_SOURCES.values())


def get_source(slug: str) -> SourceDefinition:
    try:
        return _SOURCES[slug]
    except KeyError:
        available = ", ".join(_SOURCES)
        raise ValueError(f"Unknown source {slug!r}. Available: {available}") from None


register_source(
    SourceDefinition(
        slug="linkedin",
        description="Live LinkedIn search and detail adapter; requires local Chrome",
        spider_path="job_harvester.spiders.linkedin_jobs.LinkedInJobsSpider",
        requires_browser=True,
    )
)
register_source(
    SourceDefinition(
        slug="jsonl",
        description="Offline local JSONL import; no browser or network",
        spider_path="job_harvester.spiders.jsonl_jobs.JsonlJobsSpider",
        requires_input=True,
    )
)
