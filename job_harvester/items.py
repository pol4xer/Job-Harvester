from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from job_harvester.storage import build_content_hash, build_dedup_key, canonicalize_url


class Vacancy(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    source: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    source_job_id: str | None = None
    source_url: str
    canonical_url: str | None = None
    apply_url: str | None = None

    company: str
    title: str
    description: str = ""
    description_html: str | None = None

    location: str | None = None
    workplace_type: Literal["remote", "hybrid", "on-site", "unknown"] = "unknown"
    employment_type: Literal[
        "full-time", "contract", "part-time", "temporary", "internship", "unknown"
    ] = "unknown"
    posted_at: date | None = None
    applicants: str | None = None
    seniority_level: str | None = None
    job_function: str | None = None
    industries: str | None = None

    salary_min: Decimal | None = None
    salary_max: Decimal | None = None
    salary_currency: str | None = None
    salary_interval: Literal["hour", "day", "month", "year"] | None = None
    salary_text: str | None = None

    capture_status: Literal["partial", "complete"] = "complete"
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw_html: str | None = None

    search_run_id: str | None = None
    search_profile_id: str | None = None
    search_query: str | None = None
    search_location: str | None = None
    search_rank: int | None = None

    dedup_key: str | None = None
    content_hash: str | None = None

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source_url must be an absolute HTTP(S) URL")
        return value

    @model_validator(mode="after")
    def compute_identity(self) -> Vacancy:
        # An incomplete refresh must never replace a previously complete
        # snapshot, even if an adapter or imported record labels it complete.
        if not all((self.company, self.title, self.description)):
            self.capture_status = "partial"
        self.canonical_url = canonicalize_url(self.canonical_url or self.source_url)
        payload = self.model_dump(mode="json")
        self.dedup_key = build_dedup_key(payload)
        self.content_hash = build_content_hash(payload)
        if self.captured_at.tzinfo is None:
            self.captured_at = self.captured_at.replace(tzinfo=UTC)
        return self

    def as_dict(self, *, include_raw: bool = True) -> dict:
        payload = self.model_dump(mode="json")
        if not include_raw:
            payload.pop("raw_html", None)
            payload.pop("description_html", None)
        return payload
