"""Persistence regressions using fresh databases and fictional jobs only."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_harvester.storage import (
    SQLiteStorage,
    build_content_hash,
    canonicalize_url,
    load_complete_job_identities,
    normalize_job,
)


def sample_job(**changes):
    return {
        "source": "example",
        "source_job_id": "42",
        "source_url": "https://jobs.example.com/roles/42",
        "company": "Example Studio",
        "title": "Python Engineer",
        "description": "Build useful tools with Python.",
        "capture_status": "complete",
        "captured_at": "2026-09-01T09:00:00Z",
        **changes,
    }


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "jobs.sqlite3"
        self.storage = SQLiteStorage(self.database)
        self.addCleanup(self.storage.close)

    def test_same_external_id_is_scoped_to_source(self):
        first = self.storage.store_job(sample_job())
        second = self.storage.store_job(sample_job(source="another"))
        self.assertNotEqual(first.job_id, second.job_id)
        self.assertEqual(first.dedup_key, "example:id:42")
        self.assertEqual(second.dedup_key, "another:id:42")

    def test_external_dedup_key_cannot_claim_another_source(self):
        with self.assertRaisesRegex(ValueError, "scoped"):
            self.storage.store_job(sample_job(dedup_key="another:id:42"))
        self.assertEqual(
            self.storage.connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 0
        )

    def test_legacy_source_scoped_key_remains_accepted(self):
        result = self.storage.store_job(sample_job(dedup_key="example:42"))
        self.assertEqual(result.dedup_key, "example:42")

    def test_repeat_observation_updates_seen_time_without_new_snapshot(self):
        run_id = self.storage.start_search_run(source="example")
        first = self.storage.store_job(sample_job(search_rank=4), run_id=run_id)
        second = self.storage.store_job(
            sample_job(captured_at="2026-09-02T09:00:00Z", search_rank=2), run_id=run_id
        )
        self.assertEqual((first.job_id, first.snapshot_id), (second.job_id, second.snapshot_id))
        self.assertFalse(second.new_snapshot)
        self.assertFalse(second.new_hit)
        row = self.storage.connection.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(row["first_seen_at"], "2026-09-01T09:00:00Z")
        self.assertEqual(row["last_seen_at"], "2026-09-02T09:00:00Z")
        run = self.storage.connection.execute("SELECT * FROM search_runs").fetchone()
        self.assertEqual(run["new_jobs_count"], 1)
        self.assertEqual(run["new_snapshots_count"], 1)
        self.assertEqual(run["duplicate_snapshots_count"], 1)
        self.assertEqual(run["hit_count"], 1)
        hit = self.storage.connection.execute("SELECT * FROM search_hits").fetchone()
        self.assertEqual(hit["position"], 2)

    def test_content_changes_are_immutable_and_reappearance_reuses_snapshot(self):
        first = self.storage.store_job(sample_job())
        changed = self.storage.store_job(
            sample_job(description="Build APIs.", content_hash="wrong")
        )
        self.assertNotEqual(first.snapshot_id, changed.snapshot_id)
        returned = self.storage.store_job(sample_job(captured_at="2026-09-03T00:00:00Z"))
        self.assertEqual(first.snapshot_id, returned.snapshot_id)
        self.assertFalse(returned.new_snapshot)
        row = self.storage.connection.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(row["latest_snapshot_id"], first.snapshot_id)
        descriptions = {
            row[0]
            for row in self.storage.connection.execute("SELECT description FROM job_snapshots")
        }
        self.assertEqual(descriptions, {"Build useful tools with Python.", "Build APIs."})

    def test_partial_observation_keeps_last_complete_snapshot(self):
        complete = self.storage.store_job(sample_job(source="linkedin"))
        partial = self.storage.store_job(
            sample_job(
                source="linkedin",
                description="",
                capture_status="partial",
                captured_at="2026-09-02T00:00:00Z",
            )
        )
        self.storage.commit()
        row = self.storage.connection.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(row["latest_snapshot_id"], partial.snapshot_id)
        self.assertEqual(row["latest_complete_snapshot_id"], complete.snapshot_id)
        ids, urls = load_complete_job_identities(self.database)
        self.assertEqual(ids, {"42"})
        self.assertEqual(urls, {"https://jobs.example.com/roles/42"})
        self.assertEqual(
            load_complete_job_identities(self.database, source="another"), (set(), set())
        )

    def test_complete_identity_loader_ignores_blank_description(self):
        self.storage.store_job(sample_job(source="linkedin", description="  "))
        self.storage.commit()
        self.assertEqual(load_complete_job_identities(self.database), (set(), set()))

    def test_store_is_not_visible_to_other_connections_until_commit(self):
        self.storage.store_job(sample_job())
        with sqlite3.connect(self.database) as other:
            self.assertEqual(other.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.storage.commit()
        with sqlite3.connect(self.database) as other:
            self.assertEqual(other.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_rollback_undoes_entire_uncommitted_batch(self):
        self.storage.store_job(sample_job())
        self.storage.store_job(sample_job(source_job_id="43"))
        self.storage.rollback()
        self.assertEqual(
            self.storage.connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 0
        )
        self.assertEqual(
            self.storage.connection.execute("SELECT count(*) FROM job_snapshots").fetchone()[0], 0
        )

    def test_failed_item_rolls_back_only_that_item(self):
        run_id = self.storage.start_search_run(source="example")
        self.storage.commit()
        kept = self.storage.store_job(sample_job(), run_id=run_id)
        # A failed hit insert happens after the existing job's content is updated.
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.store_job(sample_job(description="Must roll back."), run_id=99999)
        self.storage.record_error(run_id)
        self.storage.commit()
        row = self.storage.connection.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(row["latest_snapshot_id"], kept.snapshot_id)
        self.assertEqual(
            self.storage.connection.execute("SELECT count(*) FROM job_snapshots").fetchone()[0], 1
        )
        self.assertEqual(
            self.storage.connection.execute("SELECT error_count FROM search_runs").fetchone()[0], 1
        )

    def test_capture_times_are_normalized_to_utc(self):
        normalized = normalize_job(sample_job(captured_at="2026-09-01T12:00:00+03:00"))
        self.assertEqual(normalized["captured_at"], "2026-09-01T09:00:00Z")

    def test_capture_metadata_does_not_change_content_hash(self):
        self.assertEqual(
            build_content_hash(sample_job()),
            build_content_hash(
                sample_job(
                    captured_at="2026-10-01T00:00:00Z",
                    search_query="new query",
                    raw_html="different",
                )
            ),
        )

    def test_linkedin_canonicalization_requires_real_domain(self):
        self.assertEqual(
            canonicalize_url(
                "https://uk.linkedin.com/jobs/view/role-123?trackingId=abc&utm_source=test"
            ),
            "https://www.linkedin.com/jobs/view/123/",
        )
        self.assertEqual(
            canonicalize_url("https://notlinkedin.com/jobs/view/123?utm_source=test"),
            "https://notlinkedin.com/jobs/view/123",
        )

    def test_context_manager_failure_rolls_back(self):
        separate_database = Path(self.directory.name) / "context.sqlite3"
        with (
            self.assertRaisesRegex(RuntimeError, "Stop batch"),
            SQLiteStorage(separate_database) as storage,
        ):
            storage.store_job(sample_job())
            raise RuntimeError("Stop batch")
        with sqlite3.connect(separate_database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_url_normalization_preserves_ipv6_and_drops_known_tracking(self):
        self.assertEqual(
            canonicalize_url("https://[2001:db8::1]:8443/jobs/42?utm_source=x"),
            "https://[2001:db8::1]:8443/jobs/42",
        )
        self.assertEqual(
            canonicalize_url(
                "https://jobs.example.com/42?midToken=fixture&lipi=fixture&department=platform"
            ),
            "https://jobs.example.com/42?department=platform",
        )

    def test_new_default_reuses_persisted_legacy_id_and_snapshots(self):
        original = self.storage.store_job(sample_job(source="linkedin", dedup_key="linkedin:42"))
        self.storage.commit()
        # Reopen a synthetic legacy database as an upgraded application would.
        with SQLiteStorage(self.database) as upgraded:
            same = upgraded.store_job(
                sample_job(
                    source="linkedin",
                    dedup_key="linkedin:id:42",
                    captured_at="2026-09-02T09:00:00Z",
                )
            )
            changed = upgraded.store_job(
                sample_job(source="linkedin", description="Updated fictional description.")
            )
            other_source = upgraded.store_job(sample_job(source="another"))
        self.assertEqual(same.job_id, original.job_id)
        self.assertEqual(same.snapshot_id, original.snapshot_id)
        self.assertEqual(same.dedup_key, "linkedin:42")
        self.assertFalse(same.new_job)
        self.assertFalse(same.new_snapshot)
        self.assertEqual(changed.job_id, original.job_id)
        self.assertNotEqual(changed.snapshot_id, original.snapshot_id)
        self.assertNotEqual(other_source.job_id, original.job_id)
        self.assertEqual(
            self.storage.connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 2
        )

    def test_new_url_default_reuses_legacy_url_key_with_trailing_slash(self):
        url = "https://jobs.example.com/role/no-id"
        original = self.storage.store_job(
            sample_job(source_job_id=None, source_url=url, dedup_key=f"example:{url}/")
        )
        self.storage.commit()
        repeated = self.storage.store_job(sample_job(source_job_id=None, source_url=url))
        self.assertEqual(repeated.job_id, original.job_id)
        self.assertEqual(repeated.dedup_key, f"example:{url}/")
        self.assertFalse(repeated.new_snapshot)

    def test_inferred_id_reuses_legacy_url_identity(self):
        url = "https://www.linkedin.com/jobs/view/42/"
        original = self.storage.store_job(
            sample_job(
                source="linkedin", source_job_id=None, source_url=url, dedup_key=f"linkedin:{url}"
            )
        )
        repeated = self.storage.store_job(sample_job(source="linkedin", source_url=url))
        self.assertEqual(repeated.job_id, original.job_id)
        self.assertEqual(repeated.dedup_key, f"linkedin:{url}")
        self.assertFalse(repeated.new_snapshot)

    def test_legacy_compatibility_does_not_merge_custom_keys(self):
        custom = self.storage.store_job(sample_job(dedup_key="example:custom:42"))
        default = self.storage.store_job(sample_job())
        another_custom = self.storage.store_job(sample_job(dedup_key="example:second-custom:42"))
        self.assertNotEqual(default.job_id, custom.job_id)
        self.assertNotEqual(default.job_id, another_custom.job_id)
        self.assertEqual(default.dedup_key, "example:id:42")
