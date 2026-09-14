"""Verify the built wheel from a temporary directory outside the checkout."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    wheels = sorted((root / "dist").glob("job_harvester-*.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        raise SystemExit("Build the wheel first with `make build`.")
    with tempfile.TemporaryDirectory(prefix="job-harvester-wheel-") as directory:
        work = Path(directory)
        installed = work / "installed"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--no-index",
                "--no-deps",
                "--target",
                str(installed),
                str(wheels[-1]),
            ],
            check=True,
            cwd=work,
        )
        # Reuse locked dependencies, but skip editable-install .pth hooks so
        # the source checkout cannot satisfy imports missing from the wheel.
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(installed), sysconfig.get_path("purelib"))),
            "JOB_HARVESTER_INSTALLED_PATH": str(installed),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        code = """
import importlib.metadata
import os
from pathlib import Path
import sys
from unittest.mock import patch
import job_harvester
assert Path(job_harvester.__file__).resolve().is_relative_to(
    Path(os.environ['JOB_HARVESTER_INSTALLED_PATH']).resolve())
entry = next(e for e in importlib.metadata.distribution('job-harvester').entry_points
             if e.group == 'console_scripts' and e.name == 'job-harvester')
with patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')):
    result = entry.load()()
assert 'seleniumbase' not in sys.modules
raise SystemExit(result)
"""
        for args in (
            ["list-sources"],
            ["list-profiles"],
            ["demo"],
            ["demo"],
            ["stats", "--database", "data/demo/jobs.sqlite3"],
        ):
            result = subprocess.run(
                [sys.executable, "-S", "-c", code, *args],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            if result.returncode:
                raise SystemExit(result.stderr or result.stdout)
        with sqlite3.connect(work / "data/demo/jobs.sqlite3") as connection:
            expected = {"jobs": 3, "job_snapshots": 3, "search_runs": 2, "search_hits": 6}
            for table, count in expected.items():
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
        assert len(json.loads((work / "data/demo/vacancies.json").read_text())) == 3
        assert not (work / ".browser-profile").exists()
        assert not (installed / "job_harvester" / "data").exists()
        print("Installed wheel: bundled config, offline demo, repeat import and exports passed.")


if __name__ == "__main__":
    main()
