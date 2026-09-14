import os
import subprocess
import sys
from pathlib import Path

import pytest

from job_harvester.runner import run_crawl
from job_harvester.sources import SourceDefinition

ROOT = Path(__file__).resolve().parents[1]


def test_unknown_source_is_rejected_before_creating_runtime_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="Unknown source"):
        run_crawl(source="not_implemented")
    assert not list(tmp_path.iterdir())


def test_adapter_source_metadata_is_required():
    definition = SourceDefinition("wrong", "Bad registration", "scrapy.Spider")
    with pytest.raises(ValueError, match="source attribute"):
        definition.load_spider()


@pytest.mark.parametrize(
    "failure_mode", ["startup", "start_iterator", "finish_reason", "wrong_item_source"]
)
def test_crawl_failure_paths_do_not_return_success(tmp_path, failure_mode):
    bodies = {
        "startup": """
    def __init__(self, **kwargs):
        raise ValueError("startup fixture failure")
""",
        "start_iterator": """
    async def start(self):
        raise ValueError("iterator fixture failure")
        yield
""",
        "finish_reason": """
    async def start(self):
        self.close_reason = "test_cancelled"
        if False:
            yield
""",
        "wrong_item_source": """
    async def start(self):
        yield {"source": "linkedin", "source_job_id": "123", "company": "Example",
               "title": "Example role", "source_url": "https://example.com/jobs/123"}
""",
    }
    (tmp_path / "failing_adapter.py").write_text(
        "import scrapy\nclass FailingSpider(scrapy.Spider):\n"
        '    name = "failing"\n    source = "failing"\n' + bodies[failure_mode]
    )
    script = """
from job_harvester.runner import run_crawl
from job_harvester.sources import SourceDefinition, register_source
register_source(SourceDefinition("failing", "Failure test", "failing_adapter.FailingSpider"))
try:
    run_crawl(source="failing", log_level="ERROR")
except (ValueError, RuntimeError):
    pass
else:
    raise AssertionError("A failed crawl returned success")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT), str(tmp_path)))},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
