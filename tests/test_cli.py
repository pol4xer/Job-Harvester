import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from job_harvester.cli import main
from job_harvester.config import DEFAULT_CONFIG, load_config, resolve_project_path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(tmp_path, *args):
    return subprocess.run(
        [sys.executable, "-m", "job_harvester.cli", *args],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_list_sources_is_honest_and_does_not_import_browser(capsys):
    assert main(["list-sources"]) == 0
    output = capsys.readouterr().out
    assert "linkedin" in output and "jsonl" in output
    assert "greenhouse" not in output.lower()


def test_bundled_profiles_work_outside_checkout_and_keep_legacy_profile(tmp_path):
    result = run_cli(tmp_path, "list-profiles")
    assert result.returncode == 0, result.stderr
    assert "senior_python" in result.stdout
    assert not list(tmp_path.iterdir())


def test_bundled_runtime_paths_use_cwd_and_explicit_configs_use_their_directory(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    config, path = load_config()
    assert path == DEFAULT_CONFIG
    assert resolve_project_path(config.database, path) == tmp_path / "data/jobs.sqlite3"
    custom_path = tmp_path / "config-dir" / "searches.yaml"
    assert resolve_project_path(config.database, custom_path) == (
        tmp_path / "config-dir/data/jobs.sqlite3"
    )


def test_cli_rejects_missing_jsonl_input_without_creating_data(tmp_path):
    result = run_cli(tmp_path, "crawl", "--source", "jsonl")
    assert result.returncode == 1
    assert "requires a local --input" in result.stderr
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / ".browser-profile").exists()


def test_cli_invalid_jsonl_fails_without_echoing_record_values(tmp_path):
    source = tmp_path / "input.jsonl"
    source.write_text('{"title": "PRIVATE_SENTINEL", "source_url": "not-a-url"}\n')
    result = run_cli(tmp_path, "crawl", "--source", "jsonl", "--input", str(source))
    assert result.returncode == 1
    assert "Invalid JSONL vacancy on line 1" in result.stderr
    assert "PRIVATE_SENTINEL" not in result.stderr
    with sqlite3.connect(tmp_path / "data/jobs.sqlite3") as connection:
        assert connection.execute("SELECT status, close_reason FROM search_runs").fetchone() == (
            "failed",
            "invalid_input",
        )


def test_jsonl_import_owns_source_identity_and_obeys_limit(tmp_path):
    source = tmp_path / "input.jsonl"
    source.write_text("""{"source":"linkedin","dedup_key":"linkedin:id:1","source_job_id":"1","source_url":"https://example.com/1","company":"Example","title":"Example"}
{"source_job_id":"2","source_url":"https://example.com/2","company":"Example","title":"Example"}
""")
    result = run_cli(
        tmp_path, "crawl", "--source", "jsonl", "--input", str(source), "--max-results", "1"
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(tmp_path / "data/jobs.sqlite3") as connection:
        assert connection.execute("SELECT source, dedup_key FROM jobs").fetchall() == [
            ("jsonl", "jsonl:id:1")
        ]
