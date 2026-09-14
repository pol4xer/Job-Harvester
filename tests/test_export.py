"""Exports must preserve complete content and safely replace output files."""

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_harvester.export import export_database
from job_harvester.storage import SQLiteStorage


def job(**changes):
    return {
        "source": "example",
        "source_job_id": "1",
        "source_url": "https://jobs.example.com/1",
        "company": "Example Studio",
        "title": "Engineer",
        "description": "A fictional vacancy.",
        "captured_at": "2026-09-01T09:00:00Z",
        "capture_status": "complete",
        **changes,
    }


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "jobs.sqlite3"
        self.output = self.root / "export.json"

    def store(self, *jobs):
        with SQLiteStorage(self.database) as storage:
            for item in jobs:
                storage.store_job(item)

    def test_latest_complete_snapshot_wins_over_newer_partial(self):
        self.store(
            job(), job(description="", capture_status="partial", captured_at="2026-09-02T00:00:00Z")
        )
        self.assertEqual(export_database(self.database, self.output, complete_only=True), 1)
        record = json.loads(self.output.read_text())[0]
        self.assertEqual(record["description"], "A fictional vacancy.")
        self.assertEqual(record["capture_status"], "complete")
        self.assertEqual(record["last_seen_at"], "2026-09-02T00:00:00Z")
        self.assertNotIn("raw_json", record)

    def test_complete_only_excludes_partial_and_mislabeled_empty_jobs(self):
        self.store(
            job(),
            job(source_job_id="2", description="", capture_status="partial"),
            job(source_job_id="3", description="", capture_status="complete"),
        )
        self.assertEqual(export_database(self.database, self.output, complete_only=True), 1)
        self.assertEqual(export_database(self.database, self.output), 3)

    def test_limit_since_and_raw_are_applied(self):
        self.store(
            job(captured_at="2026-08-30T00:00:00Z"),
            job(source_job_id="2", captured_at="2026-09-02T00:00:00Z"),
        )
        self.assertEqual(
            export_database(
                self.database, self.output, since="2026-09-01", limit=1, include_raw=True
            ),
            1,
        )
        row = json.loads(self.output.read_text())[0]
        self.assertEqual(row["source_job_id"], "2")
        self.assertEqual(row["raw_json"]["company"], "Example Studio")
        self.assertEqual(export_database(self.database, self.output, limit=0), 0)
        self.assertEqual(json.loads(self.output.read_text()), [])

    def test_since_compares_instants_for_legacy_offsets(self):
        self.store(job())
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE jobs SET last_seen_at = '2026-09-01T12:00:00+03:00'")
        self.assertEqual(
            export_database(self.database, self.output, since="2026-09-01T10:00:00Z"), 0
        )
        self.assertEqual(
            export_database(self.database, self.output, since="2026-09-01T05:00:00-04:00"), 1
        )

    def test_csv_escapes_formula_and_control_prefixes(self):
        for value in ["=1+2", "+1+2", "-1+2", "@SUM(A1)", "  =1+2", "\t=1+2", "\r=1+2"]:
            with self.subTest(value=value):
                self.store(job(company=value, description=value))
                # Legacy/imported rows may retain leading whitespace and controls.
                with sqlite3.connect(self.database) as connection:
                    connection.execute(
                        "UPDATE job_snapshots SET company = ?, description = ?", (value, value)
                    )
                export_database(self.database, self.output, format="csv")
                with self.output.open(newline="") as stream:
                    record = next(csv.DictReader(stream))
                self.assertTrue(record["company"].startswith("'"))
                self.assertTrue(record["description"].startswith("'"))
                self.assertEqual(record["source_url"], "https://jobs.example.com/1")

    def test_tracker_defaults_are_english_and_legacy_is_opt_in(self):
        self.store(
            job(
                workplace_type="remote",
                employment_type="contract",
                salary_min=60000,
                salary_currency="USD",
                salary_interval="year",
            )
        )
        export_database(self.database, self.output, format="tracker-csv")
        with self.output.open(newline="") as stream:
            record = next(csv.DictReader(stream))
        self.assertEqual(len(record), 26)
        self.assertEqual(record["Company"], "Example Studio")
        self.assertEqual(record["Status"], "New")
        self.assertEqual(record["Workplace"], "Remote")
        self.assertEqual(record["Salary / currency"], "from 60 000 USD / year")
        export_database(self.database, self.output, format="tracker-csv-ru")
        with self.output.open(newline="") as stream:
            record = next(csv.DictReader(stream))
        self.assertEqual(record["Компания"], "Example Studio")
        self.assertEqual(record["Статус"], "Новая")

    def test_failed_write_preserves_existing_output_and_cleans_temporary_file(self):
        self.store(job())
        self.output.write_text("previous export\n")

        def fail_after_partial_write(stream, *args):
            stream.write("incomplete output")
            raise OSError("Synthetic disk write failure")

        with (
            patch("job_harvester.export._write_rows", side_effect=fail_after_partial_write),
            self.assertRaisesRegex(OSError, "Synthetic"),
        ):
            export_database(self.database, self.output)
        self.assertEqual(self.output.read_text(), "previous export\n")
        self.assertEqual(list(self.root.glob(".export.json.*.tmp")), [])

    def test_export_cannot_replace_database(self):
        self.store(job())
        with self.assertRaisesRegex(ValueError, "overwrite"):
            export_database(self.database, self.database)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_invalid_filters_leave_previous_output_unchanged(self):
        self.store(job())
        self.output.write_text("previous export")
        for options in [{"since": "not a date"}, {"limit": -1}, {"format": "xlsx"}]:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    export_database(self.database, self.output, **options)
                self.assertEqual(self.output.read_text(), "previous export")

    def test_jsonl_exports_structured_fields_and_numeric_salary(self):
        self.store(job(industries=["Software", "Education"], salary_min="1234.50"))
        export_database(self.database, self.output, format="jsonl")
        row = json.loads(self.output.read_text())
        self.assertEqual(row["industries"], ["Software", "Education"])
        self.assertEqual(row["salary_min"], 1234.5)
