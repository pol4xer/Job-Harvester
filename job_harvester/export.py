"""Atomic exports of the latest stored snapshot for every unique job."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TextIO

EXPORT_FIELDS = (
    "job_id",
    "snapshot_id",
    "dedup_key",
    "content_hash",
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
    "industries",
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
    "captured_at",
    "first_seen_at",
    "last_seen_at",
)

# Optional compatibility with existing Russian-language application trackers.
TRACKER_HEADERS_RU = (
    "ID",
    "Компания",
    "Вакансия",
    "Ссылка",
    "Локация",
    "Формат",
    "Тип занятости",
    "Вилка / валюта",
    "Запрос USD/мес",
    "Fit",
    "Приоритет",
    "Статус",
    "Добавлена",
    "Отклик",
    "Следующий шаг",
    "Follow-up",
    "Контроль",
    "CV / материалы",
    "Ключевые требования",
    "Сильные совпадения",
    "Пробелы / риски",
    "Изменения CV",
    "Заметки",
    "Outreach / контакт",
    "Последнее обновление",
    "Результат / причина",
)

TRACKER_HEADERS = (
    "ID",
    "Company",
    "Role",
    "URL",
    "Location",
    "Workplace",
    "Employment type",
    "Salary / currency",
    "Target USD / month",
    "Fit",
    "Priority",
    "Status",
    "Added",
    "Applied",
    "Next step",
    "Follow-up",
    "Review date",
    "CV / materials",
    "Key requirements",
    "Strong matches",
    "Gaps / risks",
    "CV changes",
    "Notes",
    "Outreach / contact",
    "Last updated",
    "Outcome / reason",
)

_LATEST_JOBS_SQL = """
SELECT
    j.id AS job_id,
    s.id AS snapshot_id,
    j.dedup_key,
    s.content_hash,
    s.source,
    s.source_job_id,
    COALESCE(s.source_url, s.canonical_url) AS source_url,
    s.canonical_url,
    s.apply_url,
    s.company,
    s.company_url,
    s.company_logo_url,
    s.title,
    s.location,
    s.workplace_type,
    s.employment_type,
    s.seniority_level,
    s.job_function,
    s.industries_json,
    s.posted_at,
    s.valid_through,
    s.salary_min,
    s.salary_max,
    s.salary_currency,
    s.salary_interval,
    s.salary_text,
    s.description,
    s.description_html,
    s.applicants,
    s.applicants_count,
    s.capture_status,
    s.captured_at,
    s.raw_json,
    j.first_seen_at,
    j.last_seen_at
FROM jobs AS j
JOIN job_snapshots AS s ON s.id = (
    COALESCE(
        j.latest_complete_snapshot_id,
        (
            SELECT latest.id
            FROM job_snapshots AS latest
            WHERE latest.id = j.latest_snapshot_id
              AND latest.capture_status = 'complete'
        ),
        (
            SELECT candidate.id
            FROM job_snapshots AS candidate
            WHERE candidate.job_id = j.id
              AND candidate.capture_status = 'complete'
            ORDER BY candidate.captured_at DESC, candidate.id DESC
            LIMIT 1
        ),
        j.latest_snapshot_id,
        (
            SELECT fallback.id
            FROM job_snapshots AS fallback
            WHERE fallback.job_id = j.id
            ORDER BY fallback.captured_at DESC, fallback.id DESC
            LIMIT 1
        )
    )
)
"""


def export_database(
    database_path: str | Path,
    output_path: str | Path,
    *,
    format: str | None = None,
    limit: int | None = None,
    since: str | date | datetime | None = None,
    complete_only: bool = False,
    include_raw: bool = False,
) -> int:
    """Export latest snapshots atomically and return the written row count.

    ``since`` filters by the identity's ``last_seen_at`` value.  Formats are
    ``jsonl``, ``json``, ``csv``, ``tracker-csv`` (English), and
    ``tracker-csv-ru`` (legacy Russian tracker compatibility).
    """

    db_path = Path(database_path).expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {db_path}")
    destination = Path(output_path).expanduser().resolve()
    if destination == db_path:
        raise ValueError("output_path must not overwrite the SQLite database")
    export_format = _resolve_format(format, destination)
    if limit is not None and int(limit) < 0:
        raise ValueError("limit must be zero or greater")

    # Let SQLite initialise WAL/SHM sidecars when a portable copy has none,
    # while still preventing every write through this connection.
    connection = sqlite3.connect(db_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        rows = iter_latest_jobs(
            connection,
            limit=limit,
            since=since,
            complete_only=complete_only,
            include_raw=include_raw,
        )
        with _atomic_text_file(destination) as output:
            count = _write_rows(output, rows, export_format, include_raw)
    finally:
        connection.close()
    return count


def iter_latest_jobs(
    connection: sqlite3.Connection,
    *,
    limit: int | None = None,
    since: str | date | datetime | None = None,
    complete_only: bool = False,
    include_raw: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield normalized dictionaries for the latest immutable snapshots."""

    where: list[str] = []
    parameters: list[Any] = []
    if since is not None:
        where.append("julianday(j.last_seen_at) >= julianday(?)")
        parameters.append(_normalize_since(since))
    if complete_only:
        where.extend(
            (
                "COALESCE(TRIM(s.company), '') <> ''",
                "COALESCE(TRIM(s.title), '') <> ''",
                "COALESCE(TRIM(s.description), '') <> ''",
                "COALESCE(TRIM(s.source_url), TRIM(s.canonical_url), '') <> ''",
                "s.capture_status = 'complete'",
            )
        )

    query = _LATEST_JOBS_SQL
    if where:
        query += "\nWHERE " + " AND ".join(where)
    query += "\nORDER BY j.last_seen_at DESC, s.captured_at DESC, j.id DESC"
    if limit is not None:
        query += "\nLIMIT ?"
        parameters.append(int(limit))

    for row in connection.execute(query, parameters):
        result = dict(row)
        result["industries"] = _decode_json(result.pop("industries_json", None))
        result["job_function"] = _decode_json(result.get("job_function"))
        result["salary_min"] = _json_number(result.get("salary_min"))
        result["salary_max"] = _json_number(result.get("salary_max"))
        if include_raw:
            result["raw_json"] = _decode_json(result.get("raw_json"))
        else:
            result.pop("raw_json", None)
        yield result


def _write_rows(
    output: TextIO,
    rows: Iterable[dict[str, Any]],
    export_format: str,
    include_raw: bool,
) -> int:
    if export_format == "jsonl":
        count = 0
        for row in rows:
            output.write(_json_dumps(row))
            output.write("\n")
            count += 1
        return count

    if export_format == "json":
        count = 0
        output.write("[\n")
        for row in rows:
            if count:
                output.write(",\n")
            output.write(json.dumps(row, ensure_ascii=False, indent=2))
            count += 1
        output.write("\n]\n")
        return count

    if export_format in {"tracker-csv", "tracker-csv-ru"}:
        legacy = export_format == "tracker-csv-ru"
        writer = csv.DictWriter(
            output,
            fieldnames=TRACKER_HEADERS_RU if legacy else TRACKER_HEADERS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        count = 0
        for row in rows:
            writer.writerow(
                {
                    key: _safe_csv_cell(value)
                    for key, value in _tracker_row(row, legacy=legacy).items()
                }
            )
            count += 1
        return count

    fields = list(EXPORT_FIELDS)
    if include_raw:
        fields.append("raw_json")
    writer = csv.DictWriter(
        output,
        fieldnames=fields,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    count = 0
    for row in rows:
        writer.writerow({field: _safe_csv_cell(row.get(field)) for field in fields})
        count += 1
    return count


def _tracker_row(row: Mapping[str, Any], *, legacy: bool = False) -> dict[str, Any]:
    added_at = _date_part(row.get("first_seen_at") or row.get("captured_at"))
    updated_at = _date_part(row.get("last_seen_at") or row.get("captured_at"))
    translated = {
        "ID": row.get("source_job_id") or row.get("dedup_key"),
        "Компания": row.get("company"),
        "Вакансия": row.get("title"),
        "Ссылка": row.get("source_url") or row.get("canonical_url"),
        "Локация": row.get("location"),
        "Формат": _localized_workplace(row.get("workplace_type"), legacy=legacy),
        "Тип занятости": _localized_employment(row.get("employment_type"), legacy=legacy),
        "Вилка / валюта": _salary_label(row, legacy=legacy),
        "Запрос USD/мес": "",
        "Fit": "",
        "Приоритет": "",
        "Статус": "Новая" if legacy else "New",
        "Добавлена": added_at,
        "Отклик": "",
        "Следующий шаг": "",
        "Follow-up": "",
        "Контроль": "",
        "CV / материалы": "",
        "Ключевые требования": "",
        "Сильные совпадения": "",
        "Пробелы / риски": "",
        "Изменения CV": "",
        "Заметки": "",
        "Outreach / контакт": "",
        "Последнее обновление": updated_at,
        "Результат / причина": "",
    }

    if legacy:
        return translated
    return {
        english: translated[russian]
        for english, russian in zip(TRACKER_HEADERS, TRACKER_HEADERS_RU, strict=True)
    }


def _localized_workplace(value: Any, *, legacy: bool = False) -> str:
    text = str(value or "").strip()
    return {
        "remote": "Удалённо" if legacy else "Remote",
        "hybrid": "Гибрид" if legacy else "Hybrid",
        "on-site": "Офис" if legacy else "On-site",
        "onsite": "Офис" if legacy else "On-site",
        "unknown": "Не указано" if legacy else "Not specified",
    }.get(text.casefold(), text)


def _localized_employment(value: Any, *, legacy: bool = False) -> str:
    text = str(value or "").strip()
    return {
        "full-time": "Полная занятость" if legacy else "Full-time",
        "contract": "Контракт" if legacy else "Contract",
        "part-time": "Частичная занятость" if legacy else "Part-time",
        "temporary": "Временная" if legacy else "Temporary",
        "internship": "Стажировка" if legacy else "Internship",
        "unknown": "Не указано" if legacy else "Not specified",
    }.get(text.casefold(), text)


def _salary_label(row: Mapping[str, Any], *, legacy: bool = False) -> str:
    minimum = row.get("salary_min")
    maximum = row.get("salary_max")
    currency = str(row.get("salary_currency") or "").upper()
    interval = str(row.get("salary_interval") or "").casefold()
    if minimum is None and maximum is None:
        return str(row.get("salary_text") or "")
    if minimum is not None and maximum is not None:
        amount = f"{_format_number(minimum)}–{_format_number(maximum)}"
    elif minimum is not None:
        amount = f"{'от' if legacy else 'from'} {_format_number(minimum)}"
    else:
        amount = f"{'до' if legacy else 'up to'} {_format_number(maximum)}"
    interval_label = (
        {
            "hour": "час",
            "day": "день",
            "week": "неделю",
            "month": "месяц",
            "year": "год",
        }.get(interval, interval)
        if legacy
        else interval
    )
    suffix = " ".join(
        part for part in (currency, f"/ {interval_label}" if interval_label else "") if part
    )
    return f"{amount} {suffix}".strip()


def _format_number(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if number == number.to_integral():
        return f"{int(number):,}".replace(",", " ")
    return format(number.normalize(), "f")


def _safe_csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    text = _json_dumps(value) if isinstance(value, (dict, list, tuple)) else str(value)
    # Prevent spreadsheet formula injection while keeping URLs and normal text intact.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return f"'{text}"
    return text


def _decode_json(value: Any) -> Any:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _json_number(value: Any) -> int | float | str | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if not number.is_finite():
        return str(value)
    if number == number.to_integral():
        return int(number)
    return float(number)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _date_part(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _normalize_since(value: str | date | datetime) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("since cannot be empty")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                "since must be an ISO date or datetime, for example 2026-08-01"
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_format(value: str | None, output_path: Path) -> str:
    if value:
        normalized = value.strip().lower().replace("_", "-")
    else:
        suffix = output_path.suffix.lower()
        normalized = {".jsonl": "jsonl", ".json": "json", ".csv": "csv"}.get(
            suffix,
            "jsonl",
        )
    aliases = {"tracker": "tracker-csv", "sheet": "tracker-csv"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"jsonl", "json", "csv", "tracker-csv", "tracker-csv-ru"}:
        raise ValueError(f"Unsupported export format: {value!r}")
    return normalized


@contextmanager
def _atomic_text_file(destination: Path) -> Iterator[TextIO]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="",
        ) as output:
            yield output
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export the latest version of each job from SQLite."
    )
    parser.add_argument(
        "database_path",
        nargs="?",
        help="SQLite database path",
    )
    parser.add_argument(
        "--database",
        dest="database_option",
        help="SQLite database path (default: data/jobs.sqlite3)",
    )
    parser.add_argument("-o", "--output", required=True, help="Output file path")
    parser.add_argument(
        "-f",
        "--format",
        choices=("jsonl", "json", "csv", "tracker-csv", "tracker-csv-ru"),
        help="Defaults to the output extension (CSV means normalized CSV)",
    )
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument(
        "--since",
        help="Only jobs last seen on/after this ISO date or datetime",
    )
    parser.add_argument(
        "--complete-only",
        action="store_true",
        help="Require URL, company, title, and non-empty description",
    )
    parser.add_argument(
        "--include-raw",
        action="store_true",
        help="Include the original raw JSON in JSON/normalized CSV exports",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    count = export_database(
        args.database_option or args.database_path or "data/jobs.sqlite3",
        args.output,
        format=args.format,
        limit=args.limit,
        since=args.since,
        complete_only=args.complete_only,
        include_raw=args.include_raw,
    )
    print(f"Exported {count} jobs to {Path(args.output).expanduser().resolve()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "EXPORT_FIELDS",
    "TRACKER_HEADERS",
    "build_arg_parser",
    "export_database",
    "iter_latest_jobs",
    "main",
]
