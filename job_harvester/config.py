from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DatePosted = Literal["any", "past_24_hours", "past_week", "past_month"]
SortBy = Literal["relevant", "recent"]
DEFAULT_CONFIG = Path(__file__).resolve().parent / "resources" / "searches.yaml"


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_posted: DatePosted = "past_week"
    experience_levels: list[str] = Field(default_factory=lambda: ["mid_senior"])
    workplace_types: list[str] = Field(default_factory=lambda: ["remote"])
    employment_types: list[str] = Field(default_factory=lambda: ["full_time", "contract"])
    sort_by: SortBy = "recent"


class SearchDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: str = "Worldwide"
    max_results: int = Field(default=500, ge=1, le=5000)
    page_size: int = Field(default=25, ge=1, le=100)
    filters: SearchFilters = Field(default_factory=SearchFilters)


class SearchProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    keywords: str
    location: str | None = None
    max_results: int | None = Field(default=None, ge=1, le=5000)
    enabled: bool = True
    filters: SearchFilters | None = None

    @field_validator("keywords", "location")
    @classmethod
    def strip_nonempty_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


class BrowserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_dir: str = ".browser-profile"
    headless: bool = False
    incognito: bool = False
    use_uc: bool = False
    locale_code: str = "en-US"
    window_size: str = "1440,1000"
    page_load_timeout: int = Field(default=45, ge=5, le=180)
    wait_timeout: int = Field(default=20, ge=2, le=120)
    scroll_pause_min: float = Field(default=1.0, ge=0.1, le=30)
    scroll_pause_max: float = Field(default=2.0, ge=0.1, le=60)
    stagnation_limit: int = Field(default=3, ge=1, le=20)
    max_scroll_rounds: int = Field(default=250, ge=1, le=5000)
    search_deadline_seconds: int = Field(default=900, ge=10, le=7200)
    debug_pause_after_first_request_seconds: float = Field(
        default=0,
        ge=0,
        le=3600,
    )

    @model_validator(mode="after")
    def validate_pause_range(self) -> BrowserConfig:
        if self.scroll_pause_max < self.scroll_pause_min:
            raise ValueError("scroll_pause_max must be >= scroll_pause_min")
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: str = "data/jobs.sqlite3"
    refresh_existing: bool = False
    defaults: SearchDefaults = Field(default_factory=SearchDefaults)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    searches: list[SearchProfile]

    @model_validator(mode="after")
    def ensure_unique_profile_ids(self) -> AppConfig:
        ids = [profile.id for profile in self.searches]
        if len(ids) != len(set(ids)):
            raise ValueError("search profile ids must be unique")
        return self

    def get_profile(self, profile_id: str) -> SearchProfile:
        for profile in self.searches:
            if profile.id == profile_id:
                return profile
        available = ", ".join(profile.id for profile in self.searches)
        raise KeyError(f"Unknown search profile {profile_id!r}. Available: {available}")


def load_config(path: str | Path | None = None) -> tuple[AppConfig, Path]:
    config_path = Path(path or DEFAULT_CONFIG).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as stream:
        payload = yaml.safe_load(stream) or {}
    config = AppConfig.model_validate(payload)
    return config, config_path


def resolve_project_path(value: str | Path, config_path: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    if config_path.resolve() == DEFAULT_CONFIG.resolve():
        # Bundled defaults are read-only package data, not a runtime directory.
        return (Path.cwd() / path).resolve()
    config_directory = config_path.parent
    bundled_project_root = config_directory.parent
    if config_directory.name == "config" and (bundled_project_root / "pyproject.toml").is_file():
        base_directory = bundled_project_root
    else:
        base_directory = config_directory
    return (base_directory / path).resolve()
