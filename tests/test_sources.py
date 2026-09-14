import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from job_harvester.items import Vacancy
from job_harvester.sources import SourceDefinition, get_source, list_sources, register_source
from job_harvester.storage import build_content_hash, build_dedup_key

ROOT = Path(__file__).resolve().parents[1]


def run_python(tmp_path, script):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT), str(tmp_path)))}
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_registry_only_advertises_implemented_adapters():
    assert {source.slug for source in list_sources()} == {"linkedin", "jsonl"}
    assert get_source("linkedin").requires_browser
    assert not get_source("jsonl").requires_browser
    with pytest.raises(ValueError, match="Unknown source"):
        get_source("greenhouse")


def test_registration_rejects_overrides_and_invalid_slugs():
    with pytest.raises(ValueError, match="already registered"):
        register_source(get_source("jsonl"))
    with pytest.raises(ValueError, match="Source slug"):
        register_source(SourceDefinition("Bad Source", "Example", "example.Spider"))


def test_new_slug_shares_storage_identity_and_content_hash():
    item = Vacancy(
        source="custom_board",
        source_job_id="42",
        source_url="https://example.com/jobs/42?utm_source=demo",
        company="Example",
        title="Engineer",
        description="Fictional example",
    )
    assert item.dedup_key == "custom_board:id:42"
    assert item.dedup_key == build_dedup_key(item.as_dict())
    assert item.content_hash == build_content_hash(item.as_dict())


@pytest.mark.parametrize("missing_field", ["company", "title", "description"])
def test_incomplete_vacancy_cannot_claim_complete(missing_field):
    payload = {
        "source": "jsonl",
        "source_url": "https://example.com/jobs/42",
        "company": "Example",
        "title": "Engineer",
        "description": "Full fictional job description",
        "capture_status": "complete",
    }
    payload[missing_field] = "   "
    vacancy = Vacancy.model_validate(payload)
    assert vacancy.capture_status == "partial"
    assert vacancy.content_hash == build_content_hash(vacancy.as_dict())


@pytest.mark.parametrize(
    "incomplete_fields",
    [{}, {"description": ""}, {"description": "   ", "capture_status": "complete"}],
    ids=["omitted", "empty", "blank_claiming_complete"],
)
def test_jsonl_partial_refresh_preserves_previous_complete_export(tmp_path, incomplete_fields):
    base_record = {
        "source_job_id": "42",
        "source_url": "https://example.com/jobs/42",
        "company": "Example",
        "title": "Engineer",
    }
    full_record = {**base_record, "description": "Full fictional job description"}
    for record in (full_record, {**base_record, **incomplete_fields}):
        (tmp_path / "input.jsonl").write_text(json.dumps(record) + "\n")
        result = run_python(
            tmp_path,
            """
from job_harvester.runner import run_crawl
from job_harvester.export import export_database
run_crawl(source="jsonl", input_path="input.jsonl", database_path="jobs.sqlite3", log_level="ERROR")
assert export_database("jobs.sqlite3", "jobs.json", format="json") == 1
""",
        )
        assert result.returncode == 0, result.stderr
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert connection.execute(
            "SELECT capture_status FROM job_snapshots ORDER BY id"
        ).fetchall() == [("complete",), ("partial",)]
    exported = json.loads((tmp_path / "jobs.json").read_text())
    assert exported[0]["description"] == full_record["description"]
    assert exported[0]["capture_status"] == "complete"


def test_demo_uses_real_pipeline_without_browser_or_network(tmp_path):
    script = """
import sys
from unittest.mock import patch
from job_harvester.demo import run_demo
with patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')):
    with patch('job_harvester.browser.SeleniumBaseSessionProvider.__enter__',
               side_effect=AssertionError('Browser forbidden')):
        result = run_demo()
        assert result.items_processed == 4
        assert result.exported_jobs == 3
assert 'seleniumbase' not in sys.modules
"""
    result = run_python(tmp_path, script)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".browser-profile").exists()
    database = tmp_path / "data/demo/jobs.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM job_snapshots").fetchone()[0] == 3
        assert connection.execute("SELECT source, status FROM search_runs").fetchall() == [
            ("jsonl", "finished")
        ]
    exported = json.loads((tmp_path / "data/demo/vacancies.json").read_text())
    assert len(exported) == 3
    assert {row["source"] for row in exported} == {"jsonl"}


def test_reimport_adds_run_without_duplicate_jobs_or_snapshots(tmp_path):
    for _ in range(2):
        result = run_python(tmp_path, "from job_harvester.demo import run_demo; run_demo()")
        assert result.returncode == 0, result.stderr
    with sqlite3.connect(tmp_path / "data/demo/jobs.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM job_snapshots").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0] == 6


def test_registering_a_custom_spider_needs_no_storage_or_export_changes(tmp_path):
    (tmp_path / "custom_adapter.py").write_text("""
import scrapy
from job_harvester.items import Vacancy
class ExampleSpider(scrapy.Spider):
    name = "example_board"
    source = "example_board"
    async def start(self):
        yield Vacancy(source=self.source, source_job_id="demo-101",
            source_url="https://example.com/custom/101", company="Example",
            title="Custom source vacancy", description="Fictional").as_dict()
""")
    result = run_python(
        tmp_path,
        """
from job_harvester.sources import register_source, SourceDefinition
from job_harvester.runner import run_crawl
from job_harvester.export import export_database
register_source(SourceDefinition("example_board", "Fictional test", "custom_adapter.ExampleSpider"))
result = run_crawl(source="example_board", database_path="custom.sqlite3", log_level="ERROR")
assert result.source == "example_board"
assert result.profile_dir is None
assert export_database(result.database_path, "custom.json", format="json") == 1
""",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "custom.json").read_text())[0]["source"] == "example_board"
    with sqlite3.connect(tmp_path / "custom.sqlite3") as connection:
        assert connection.execute("SELECT source FROM search_runs").fetchone() == ("example_board",)
