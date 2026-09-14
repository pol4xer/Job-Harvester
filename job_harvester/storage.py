"""SQLite persistence and deterministic identity helpers for harvested jobs.

The database deliberately separates a stable job identity from immutable content
snapshots.  Seeing the same vacancy again only updates ``jobs.last_seen_at``;
changing any normalized content creates another snapshot and keeps the old one.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_BUSY_TIMEOUT_MS = 10_000
SCHEMA_VERSION = 2

_LINKEDIN_JOB_ID_RE = re.compile(r"/jobs/view/(?:[^/?#]*-)?(?P<id>\d+)(?:[/??#]|$)")
_TRACKING_QUERY_KEYS = {
    "currentjobid",
    "e_bp",
    "mcid",
    "midtoken",
    "lipi",
    "pagenum",
    "recommendedflavor",
    "originalsubdomain",
    "position",
    "refid",
    "referencid",
    "trackingid",
    "trk",
}

SNAPSHOT_FIELDS = (
    "source",
    "source_job_id",
    "source_url",
    "canonical_url",
    "apply_url",
    "company",
    "company_url",
    "company_logo_url",
    "title",
    "location",
    "workplace_type",
    "employment_type",
    "seniority_level",
    "job_function",
    "industries_json",
    "posted_at",
    "valid_through",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_interval",
    "salary_text",
    "description",
    "description_html",
    "applicants",
    "applicants_count",
    "capture_status",
)
HASH_FIELDS = SNAPSHOT_FIELDS


@dataclass(frozen=True)
class StoreResult:
    """Outcome of one idempotent job write."""

    job_id: int
    snapshot_id: int
    dedup_key: str
    content_hash: str
    new_job: bool
    new_snapshot: bool
    new_hit: bool


def load_complete_job_identities(
    database_path: str | Path,
    *,
    source: str = "linkedin",
) -> tuple[set[str], set[str]]:
    """Load IDs and canonical URLs that already have a full description.

    The connection is query-only. Missing databases are normal on the first run
    and simply produce empty identity sets.
    """

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        return set(), set()

    connection = sqlite3.connect(path, timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        rows = connection.execute(
            """
            SELECT
                jobs.source_job_id,
                COALESCE(
                    jobs.canonical_url,
                    complete_snapshot.canonical_url,
                    complete_snapshot.source_url
                ) AS canonical_url
            FROM jobs
            JOIN job_snapshots AS complete_snapshot
              ON complete_snapshot.id = jobs.latest_complete_snapshot_id
            WHERE jobs.source = ?
              AND complete_snapshot.capture_status = 'complete'
              AND length(trim(COALESCE(complete_snapshot.company, ''))) > 0
              AND length(trim(COALESCE(complete_snapshot.title, ''))) > 0
              AND length(trim(COALESCE(complete_snapshot.description, ''))) > 0
              AND length(trim(COALESCE(
                    complete_snapshot.source_url,
                    complete_snapshot.canonical_url,
                    ''
              ))) > 0
            """,
            (source.casefold(),),
        ).fetchall()
    finally:
        connection.close()

    source_job_ids = {
        str(row["source_job_id"]).strip()
        for row in rows
        if row["source_job_id"] is not None and str(row["source_job_id"]).strip()
    }
    canonical_urls = {
        normalized
        for row in rows
        if row["canonical_url"] is not None
        and (normalized := canonicalize_url(row["canonical_url"])) is not None
    }
    return source_job_ids, canonical_urls


def utc_now() -> str:
    """Return a compact, sortable UTC timestamp."""

    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def item_to_mapping(item: Any) -> dict[str, Any]:
    """Convert dict, Scrapy Item, dataclass, or Pydantic model to a plain dict."""

    if isinstance(item, Mapping):
        return dict(item)
    if hasattr(item, "model_dump"):
        try:
            return dict(item.model_dump(mode="json"))
        except TypeError:
            return dict(item.model_dump())
    if is_dataclass(item) and not isinstance(item, type):
        return asdict(item)
    if hasattr(item, "dict"):
        return dict(item.dict())
    try:
        return dict(item)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Unsupported job item type: {type(item)!r}") from exc


def canonicalize_url(value: Any) -> str | None:
    """Remove fragments/tracking parameters and normalize LinkedIn job URLs."""

    text = _text(value)
    if not text:
        return None

    if "://" not in text:
        text = f"https://{text.lstrip('/')}"
    parts = urlsplit(text)
    scheme = (parts.scheme or "https").lower()
    hostname = (parts.hostname or "").lower()
    if not hostname:
        return text

    host_for_url = f"[{hostname}]" if ":" in hostname else hostname
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host_for_url}:{port}"
    else:
        netloc = host_for_url

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    linkedin_match = _LINKEDIN_JOB_ID_RE.search(path)
    if (hostname == "linkedin.com" or hostname.endswith(".linkedin.com")) and linkedin_match:
        return f"https://www.linkedin.com/jobs/view/{linkedin_match.group('id')}/"

    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
        )
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def extract_source_job_id(source_url: Any) -> str | None:
    """Extract a LinkedIn numeric job id when it is embedded in a URL."""

    text = _text(source_url)
    if not text:
        return None
    match = _LINKEDIN_JOB_ID_RE.search(urlsplit(text).path)
    if match:
        return match.group("id")
    for key, value in parse_qsl(urlsplit(text).query):
        if key.lower() == "currentjobid" and value.isdigit():
            return value
    return None


def build_dedup_key(record: Mapping[str, Any]) -> str:
    """Build the stable, source-scoped identity used by ``jobs``."""

    source = (_text(record.get("source")) or "other").lower()
    explicit = _text(record.get("dedup_key"))
    if explicit:
        if not explicit.startswith(f"{source}:"):
            raise ValueError("dedup_key must be scoped to the job source")
        return explicit
    source_job_id = _first_text(
        record,
        "source_job_id",
        "external_id",
        "linkedin_job_id",
        "job_id",
    )
    if source_job_id:
        return f"{source}:id:{source_job_id}"

    canonical_url = canonicalize_url(
        _first_value(record, "canonical_url", "source_url", "job_url", "url")
    )
    if canonical_url:
        return f"{source}:url:{canonical_url}"
    raise ValueError("A job needs source_job_id or source_url for deduplication")


def build_content_hash(record: Mapping[str, Any]) -> str:
    """Hash normalized content while ignoring capture/search metadata.

    The database hash is authoritative and is recomputed even when a producer
    supplies its own hash. This prevents a producer with a narrower hash payload
    from hiding changes such as ``partial`` becoming ``complete``.
    """

    normalized = normalize_job(record, _calculate_hash=False)
    return _content_hash_from_normalized(normalized)


def _content_hash_from_normalized(normalized: Mapping[str, Any]) -> str:
    hash_payload = {field: _hash_value(normalized.get(field)) for field in HASH_FIELDS}
    canonical_json = json.dumps(
        hash_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def normalize_job(
    item: Any,
    *,
    _calculate_hash: bool = True,
) -> dict[str, Any]:
    """Normalize the accepted aliases into the database snapshot contract."""

    record = item_to_mapping(item)
    source = (_text(record.get("source")) or "other").lower()
    source_url = canonicalize_url(
        _first_value(record, "source_url", "job_url", "url", "canonical_url")
    )
    canonical_url = canonicalize_url(
        _first_value(record, "canonical_url", "source_url", "job_url", "url")
    )
    source_job_id = _first_text(
        record,
        "source_job_id",
        "external_id",
        "linkedin_job_id",
        "job_id",
    )
    if not source_job_id and source == "linkedin":
        source_job_id = extract_source_job_id(canonical_url or source_url)

    description = _description_text(_first_value(record, "description", "job_description"))
    normalized: dict[str, Any] = {
        "source": source,
        "source_job_id": source_job_id,
        "source_url": source_url,
        "canonical_url": canonical_url,
        "apply_url": canonicalize_url(_first_value(record, "apply_url", "application_url")),
        "company": _first_text(record, "company", "company_name"),
        "company_url": canonicalize_url(record.get("company_url")),
        "company_logo_url": _text(_first_value(record, "company_logo_url", "logo_url")),
        "title": _first_text(record, "title", "job_title"),
        "location": _first_text(record, "location", "job_location"),
        "workplace_type": _first_text(record, "workplace_type", "workplace", "remote_type"),
        "employment_type": _first_text(record, "employment_type", "job_type"),
        "seniority_level": _first_text(record, "seniority_level", "experience_level"),
        "job_function": _json_text(_first_value(record, "job_function", "job_functions")),
        "industries_json": _json_text(_first_value(record, "industries", "industry")),
        "posted_at": _iso_value(_first_value(record, "posted_at", "published_at", "date_posted")),
        "valid_through": _iso_value(_first_value(record, "valid_through", "expires_at")),
        "salary_min": _decimal_text(_first_value(record, "salary_min", "salary_from")),
        "salary_max": _decimal_text(_first_value(record, "salary_max", "salary_to")),
        "salary_currency": _first_text(record, "salary_currency", "currency"),
        "salary_interval": _first_text(record, "salary_interval", "pay_period"),
        "salary_text": _first_text(record, "salary_text", "compensation_text"),
        "description": description,
        "description_html": _description_text(
            _first_value(record, "description_html", "job_description_html")
        ),
        "applicants": _first_text(record, "applicants", "applicants_text"),
        "applicants_count": _integer(
            _first_value(
                record,
                "applicants_count",
                "applicant_count",
                "applicants",
            )
        ),
        "capture_status": _first_text(record, "capture_status")
        or ("complete" if description else "partial"),
        "captured_at": _iso_value(_first_value(record, "captured_at", "scraped_at", "collected_at"))
        or utc_now(),
        "raw_json": _raw_json(record),
    }
    normalized["dedup_key"] = build_dedup_key({**record, **normalized})
    if _calculate_hash:
        normalized["content_hash"] = _content_hash_from_normalized(normalized)
    return normalized


class SQLiteStorage:
    """Small transactional repository around the append-only SQLite schema."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = Path(database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            timeout=max(busy_timeout_ms, 1) / 1000,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS search_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_uuid TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                spider_name TEXT,
                search_profile_id TEXT,
                search_query TEXT,
                search_location TEXT,
                search_url TEXT,
                requested_limit INTEGER,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                discovered_count INTEGER NOT NULL DEFAULT 0,
                new_jobs_count INTEGER NOT NULL DEFAULT 0,
                new_snapshots_count INTEGER NOT NULL DEFAULT 0,
                duplicate_snapshots_count INTEGER NOT NULL DEFAULT 0,
                hit_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                config_json TEXT NOT NULL DEFAULT '{}',
                close_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                source_job_id TEXT,
                canonical_url TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                first_seen_run_id INTEGER REFERENCES search_runs(id),
                last_seen_run_id INTEGER REFERENCES search_runs(id),
                latest_snapshot_id INTEGER REFERENCES job_snapshots(id)
                    ON DELETE SET NULL,
                latest_complete_snapshot_id INTEGER REFERENCES job_snapshots(id)
                    ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                content_hash TEXT NOT NULL,
                source TEXT NOT NULL,
                source_job_id TEXT,
                source_url TEXT,
                canonical_url TEXT,
                apply_url TEXT,
                company TEXT,
                company_url TEXT,
                company_logo_url TEXT,
                title TEXT,
                location TEXT,
                workplace_type TEXT,
                employment_type TEXT,
                seniority_level TEXT,
                job_function TEXT,
                industries_json TEXT,
                posted_at TEXT,
                valid_through TEXT,
                salary_min TEXT,
                salary_max TEXT,
                salary_currency TEXT,
                salary_interval TEXT,
                salary_text TEXT,
                description TEXT,
                description_html TEXT,
                applicants TEXT,
                applicants_count INTEGER,
                capture_status TEXT NOT NULL DEFAULT 'complete',
                captured_at TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, content_hash)
            );

            CREATE TABLE IF NOT EXISTS search_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                snapshot_id INTEGER REFERENCES job_snapshots(id) ON DELETE SET NULL,
                search_profile_id TEXT NOT NULL DEFAULT '',
                search_query TEXT NOT NULL DEFAULT '',
                search_location TEXT NOT NULL DEFAULT '',
                search_url TEXT,
                hit_url TEXT,
                position INTEGER,
                page_number INTEGER,
                discovered_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(
                    run_id,
                    job_id,
                    search_profile_id,
                    search_query,
                    search_location
                )
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_source_id
                ON jobs(source, source_job_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_last_seen
                ON jobs(last_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_snapshots_job_captured
                ON job_snapshots(job_id, captured_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_snapshots_captured
                ON job_snapshots(captured_at DESC);
            CREATE INDEX IF NOT EXISTS idx_hits_job
                ON search_hits(job_id);
            CREATE INDEX IF NOT EXISTS idx_hits_run_position
                ON search_hits(run_id, position);
            """
        )
        job_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(jobs)")}
        if "latest_complete_snapshot_id" not in job_columns:
            self.connection.execute(
                "ALTER TABLE jobs ADD COLUMN latest_complete_snapshot_id INTEGER"
            )
        self.connection.execute(
            """
            UPDATE jobs
            SET latest_complete_snapshot_id = COALESCE(
                (
                    SELECT current.id
                    FROM job_snapshots AS current
                    WHERE current.id = jobs.latest_snapshot_id
                      AND current.capture_status = 'complete'
                ),
                (
                    SELECT candidate.id
                    FROM job_snapshots AS candidate
                    WHERE candidate.job_id = jobs.id
                      AND candidate.capture_status = 'complete'
                    ORDER BY candidate.captured_at DESC, candidate.id DESC
                    LIMIT 1
                )
            )
            WHERE latest_complete_snapshot_id IS NULL
            """
        )
        self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.connection.commit()

    def start_search_run(
        self,
        *,
        source: str = "linkedin",
        spider_name: str | None = None,
        search_profile_id: str | None = None,
        query: str | None = None,
        location: str | None = None,
        search_url: str | None = None,
        requested_limit: int | None = None,
        config: Any = None,
        run_uuid: str | None = None,
    ) -> int:
        started_at = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO search_runs (
                run_uuid, source, spider_name, search_profile_id,
                search_query, search_location, search_url, requested_limit,
                status, started_at, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                run_uuid or str(uuid.uuid4()),
                _text(source) or "other",
                _text(spider_name),
                _text(search_profile_id),
                _text(query),
                _text(location),
                canonicalize_url(search_url),
                _integer(requested_limit),
                started_at,
                _json_dumps(config if config is not None else {}),
            ),
        )
        return int(cursor.lastrowid)

    def finish_search_run(
        self,
        run_id: int,
        *,
        status: str = "finished",
        close_reason: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE search_runs
            SET status = ?, finished_at = ?, close_reason = ?
            WHERE id = ?
            """,
            (_text(status) or "finished", utc_now(), _text(close_reason), run_id),
        )

    def record_error(self, run_id: int, count: int = 1) -> None:
        self.connection.execute(
            """
            UPDATE search_runs
            SET error_count = error_count + ?
            WHERE id = ?
            """,
            (max(int(count), 0), run_id),
        )

    def store_job(self, item: Any, *, run_id: int | None = None) -> StoreResult:
        record = item_to_mapping(item)
        normalized = normalize_job(record)
        now = utc_now()
        seen_at = normalized["captured_at"]

        with self._savepoint("store_job"):
            normalized["dedup_key"] = self._resolve_existing_identity(normalized)
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    dedup_key, source, source_job_id, canonical_url,
                    first_seen_at, last_seen_at, first_seen_run_id,
                    last_seen_run_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["dedup_key"],
                    normalized["source"],
                    normalized["source_job_id"],
                    normalized["canonical_url"],
                    seen_at,
                    seen_at,
                    run_id,
                    run_id,
                    now,
                    now,
                ),
            )
            new_job = cursor.rowcount == 1
            job_row = self.connection.execute(
                "SELECT id FROM jobs WHERE dedup_key = ?",
                (normalized["dedup_key"],),
            ).fetchone()
            if job_row is None:  # pragma: no cover - defensive database guard
                raise sqlite3.IntegrityError("Failed to resolve stored job identity")
            job_id = int(job_row["id"])

            self.connection.execute(
                """
                UPDATE jobs
                SET source_job_id = COALESCE(source_job_id, ?),
                    canonical_url = COALESCE(?, canonical_url),
                    last_seen_at = CASE
                        WHEN last_seen_at < ? THEN ? ELSE last_seen_at END,
                    last_seen_run_id = COALESCE(?, last_seen_run_id),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized["source_job_id"],
                    normalized["canonical_url"],
                    seen_at,
                    seen_at,
                    run_id,
                    now,
                    job_id,
                ),
            )

            snapshot_columns = ", ".join(SNAPSHOT_FIELDS)
            placeholders = ", ".join("?" for _ in SNAPSHOT_FIELDS)
            snapshot_values = [normalized[field] for field in SNAPSHOT_FIELDS]
            cursor = self.connection.execute(
                f"""
                INSERT OR IGNORE INTO job_snapshots (
                    job_id, content_hash, {snapshot_columns},
                    captured_at, raw_json, created_at
                ) VALUES (?, ?, {placeholders}, ?, ?, ?)
                """,
                (
                    job_id,
                    normalized["content_hash"],
                    *snapshot_values,
                    normalized["captured_at"],
                    normalized["raw_json"],
                    now,
                ),
            )
            new_snapshot = cursor.rowcount == 1
            snapshot_row = self.connection.execute(
                """
                SELECT id FROM job_snapshots
                WHERE job_id = ? AND content_hash = ?
                """,
                (job_id, normalized["content_hash"]),
            ).fetchone()
            if snapshot_row is None:  # pragma: no cover - defensive database guard
                raise sqlite3.IntegrityError("Failed to resolve stored snapshot")
            snapshot_id = int(snapshot_row["id"])

            # Point at the most recently observed content even when it reuses
            # an older immutable snapshot (for example A -> B -> A).
            self.connection.execute(
                """
                UPDATE jobs
                SET latest_snapshot_id = ?,
                    latest_complete_snapshot_id = CASE
                        WHEN ? = 'complete' THEN ?
                        ELSE latest_complete_snapshot_id
                    END
                WHERE id = ?
                """,
                (
                    snapshot_id,
                    normalized["capture_status"],
                    snapshot_id,
                    job_id,
                ),
            )

            new_hit = False
            if run_id is not None:
                new_hit = self._record_hit(
                    run_id=run_id,
                    job_id=job_id,
                    snapshot_id=snapshot_id,
                    record=record,
                    normalized=normalized,
                )
                self.connection.execute(
                    """
                    UPDATE search_runs
                    SET discovered_count = discovered_count + ?,
                        new_jobs_count = new_jobs_count + ?,
                        new_snapshots_count = new_snapshots_count + ?,
                        duplicate_snapshots_count =
                            duplicate_snapshots_count + ?,
                        hit_count = hit_count + ?
                    WHERE id = ?
                    """,
                    (
                        int(new_hit),
                        int(new_job),
                        int(new_snapshot),
                        int(not new_snapshot),
                        int(new_hit),
                        run_id,
                    ),
                )

        return StoreResult(
            job_id=job_id,
            snapshot_id=snapshot_id,
            dedup_key=normalized["dedup_key"],
            content_hash=normalized["content_hash"],
            new_job=new_job,
            new_snapshot=new_snapshot,
            new_hit=new_hit,
        )

    def _resolve_existing_identity(self, normalized: Mapping[str, Any]) -> str:
        """Reuse old default keys without renaming jobs or their snapshot history.

        Earlier item models generated ``source:42`` / ``source:https://...``.
        New producers use explicit ``:id:`` and ``:url:`` namespaces. Only a
        generated default key may match an older default; caller-supplied custom
        identities retain their meaning. All lookups stay within one source.
        """
        incoming_key = str(normalized["dedup_key"])
        if incoming_key != build_dedup_key({**normalized, "dedup_key": None}):
            return incoming_key
        source = normalized["source"]
        source_job_id = normalized["source_job_id"]
        if source_job_id is not None:
            candidates = self.connection.execute(
                """
                SELECT dedup_key FROM jobs
                WHERE source = ? AND (dedup_key = ? OR source_job_id = ?)
                ORDER BY (dedup_key = ?) DESC, id
                """,
                (source, incoming_key, source_job_id, incoming_key),
            )
        else:
            candidates = self.connection.execute(
                """
                SELECT dedup_key FROM jobs
                WHERE source = ? AND (dedup_key = ? OR (
                    source_job_id IS NULL AND canonical_url = ?
                ))
                ORDER BY (dedup_key = ?) DESC, id
                """,
                (source, incoming_key, normalized["canonical_url"], incoming_key),
            )
        for row in candidates:
            key = row["dedup_key"]
            if key == incoming_key or (
                source_job_id is not None and key == f"{source}:{source_job_id}"
            ):
                return key
            if key.startswith((f"{source}:https://", f"{source}:http://")):
                return key
        return incoming_key

    def _record_hit(
        self,
        *,
        run_id: int,
        job_id: int,
        snapshot_id: int,
        record: Mapping[str, Any],
        normalized: Mapping[str, Any],
    ) -> bool:
        profile_id = _first_text(record, "search_profile_id", "search_id") or ""
        query = _first_text(record, "search_query", "query", "keywords") or ""
        location = _first_text(record, "search_location", "query_location") or ""
        metadata = _first_value(record, "search_metadata", "hit_metadata") or {}
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO search_hits (
                run_id, job_id, snapshot_id, search_profile_id,
                search_query, search_location, search_url, hit_url,
                position, page_number, discovered_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                job_id,
                snapshot_id,
                profile_id,
                query,
                location,
                canonicalize_url(record.get("search_url")),
                normalized.get("source_url"),
                _integer(
                    _first_value(
                        record,
                        "position",
                        "search_position",
                        "search_rank",
                    )
                ),
                _integer(_first_value(record, "page_number", "page")),
                _iso_value(record.get("discovered_at")) or normalized["captured_at"],
                _json_dumps(metadata),
            ),
        )
        inserted = cursor.rowcount == 1
        if not inserted:
            self.connection.execute(
                """
                UPDATE search_hits
                SET snapshot_id = ?,
                    search_url = COALESCE(?, search_url),
                    hit_url = COALESCE(?, hit_url),
                    position = CASE
                        WHEN position IS NULL THEN ?
                        WHEN ? IS NULL THEN position
                        WHEN ? < position THEN ?
                        ELSE position
                    END,
                    page_number = COALESCE(page_number, ?),
                    metadata_json = CASE
                        WHEN ? = '{}' THEN metadata_json ELSE ? END
                WHERE run_id = ? AND job_id = ?
                    AND search_profile_id = ? AND search_query = ?
                    AND search_location = ?
                """,
                (
                    snapshot_id,
                    canonicalize_url(record.get("search_url")),
                    normalized.get("source_url"),
                    _integer(
                        _first_value(
                            record,
                            "position",
                            "search_position",
                            "search_rank",
                        )
                    ),
                    _integer(
                        _first_value(
                            record,
                            "position",
                            "search_position",
                            "search_rank",
                        )
                    ),
                    _integer(
                        _first_value(
                            record,
                            "position",
                            "search_position",
                            "search_rank",
                        )
                    ),
                    _integer(
                        _first_value(
                            record,
                            "position",
                            "search_position",
                            "search_rank",
                        )
                    ),
                    _integer(_first_value(record, "page_number", "page")),
                    _json_dumps(metadata),
                    _json_dumps(metadata),
                    run_id,
                    job_id,
                    profile_id,
                    query,
                    location,
                ),
            )
        return inserted

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> SQLiteStorage:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()

    class _Savepoint:
        def __init__(self, storage: SQLiteStorage, name: str) -> None:
            self.storage = storage
            self.name = name

        def __enter__(self) -> None:
            # A top-level SAVEPOINT would commit on RELEASE. Keep an outer
            # transaction so caller-controlled commit/rollback and batches work.
            if not self.storage.connection.in_transaction:
                self.storage.connection.execute("BEGIN")
            self.storage.connection.execute(f"SAVEPOINT {self.name}")

        def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
            if exc_type is not None:
                self.storage.connection.execute(f"ROLLBACK TO {self.name}")
            self.storage.connection.execute(f"RELEASE {self.name}")
            return False

    def _savepoint(self, name: str) -> SQLiteStorage._Savepoint:
        return self._Savepoint(self, name)


def _first_value(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def _first_text(record: Mapping[str, Any], *keys: str) -> str | None:
    return _text(_first_value(record, *keys))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _description_text(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _iso_value(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, datetime):
        text = _text(value)
        if not text:
            return None
        # Preserve date-only values and unknown source date formats.
        if len(text) == 10:
            return text
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    match = re.search(r"-?\d[\d\s,.]*", str(value))
    if not match:
        return None
    digits = re.sub(r"[^\d-]", "", match.group(0))
    try:
        return int(digits)
    except ValueError:
        return None


def _decimal_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        decimal_value = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return _text(value)
    if not decimal_value.is_finite():
        return None
    return format(decimal_value.normalize(), "f")


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value, key=str)
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return _json_dumps(stripped)
        return _json_dumps(parsed)
    return _json_dumps(value)


def _raw_json(record: Mapping[str, Any]) -> str:
    supplied = record.get("raw_json", record.get("raw"))
    if supplied is None:
        supplied = record
    if isinstance(supplied, str):
        try:
            return _json_dumps(json.loads(supplied))
        except json.JSONDecodeError:
            return _json_dumps({"value": supplied})
    return _json_dumps(supplied)


def _hash_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return " ".join(value.split()).casefold()
        return parsed
    return value


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "SCHEMA_VERSION",
    "HASH_FIELDS",
    "SNAPSHOT_FIELDS",
    "SQLiteStorage",
    "StoreResult",
    "build_content_hash",
    "build_dedup_key",
    "canonicalize_url",
    "extract_source_job_id",
    "item_to_mapping",
    "load_complete_job_identities",
    "normalize_job",
    "utc_now",
]
