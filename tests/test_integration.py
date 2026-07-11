"""End-to-end integration test: real act, real docker/podman, no fakes.

Skipped automatically wherever `act` and a container runtime aren't both on
PATH -- which is everywhere except the dedicated `integration` job in CI
(see .github/workflows/ci.yml). This is the only test in the suite that
touches real infrastructure; everything else in tests/ is a pure unit test.

Written as a plain unittest.TestCase so `python -m unittest` can always
import and skip it, even when pytest itself isn't installed. The
`pytestmark` below is set conditionally so pytest can still select it with
`-m integration`, without making pytest a hard import-time dependency.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from actbreak import injector, session
from actbreak.runtime import CommandRunner, detect_runtime, require_act

try:
    import pytest

    pytestmark = pytest.mark.integration
except ImportError:  # pytest isn't installed -- fine, `unittest` doesn't need it
    pass


def _tools_available() -> bool:
    if shutil.which("act") is None:
        return False
    return shutil.which("docker") is not None or shutil.which("podman") is not None


SKIP_REASON = "requires both `act` and a container runtime (docker or podman) on PATH"

SMOKE_WORKFLOW = """\
name: actbreak integration smoke
on: push

jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - name: step one
        run: echo "step one"
      - name: step two
        run: echo "step two"
"""


@unittest.skipUnless(_tools_available(), SKIP_REASON)
class BreakBeforeIntegrationTest(unittest.TestCase):
    def test_break_before_pauses_container_then_resumes_to_completion(self):
        repo_root = tempfile.mkdtemp(prefix="actbreak-it-repo-")
        inject_dir = tempfile.mkdtemp(prefix="actbreak-it-inject-")
        proc = None
        container_name = None
        engine = detect_runtime("auto")
        runner = CommandRunner()

        try:
            workflows_dir = Path(repo_root) / ".github" / "workflows"
            workflows_dir.mkdir(parents=True)
            workflow_path = workflows_dir / "smoke.yml"
            workflow_path.write_text(SMOKE_WORKFLOW, encoding="utf-8")

            dest = str(Path(inject_dir) / "smoke.yml")
            label = injector.inject_file(str(workflow_path), dest, "smoke", 1, "before")
            self.assertEqual(label, "step two")

            act_bin = require_act()
            proc = subprocess.Popen(
                [act_bin, "-W", inject_dir, "-j", "smoke", "--reuse"],
                cwd=repo_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            container = session.wait_for_breakpoint(
                proc,
                runner,
                engine,
                job_name="smoke",
                workflow_hint="actbreak integration smoke",
                interrupt_check=lambda: None,
                timeout=300,
            )
            self.assertIsNotNone(container, "act exited before the breakpoint was ever reached")
            container_name = container.name

            self.assertTrue(
                runner.file_exists(engine, container.id, "/tmp/actbreak/hold"),
                "container was found but the hold sentinel file is missing",
            )

            runner.rm_file(engine, container.id, "/tmp/actbreak/hold")
            exit_code = proc.wait(timeout=300)
            output = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
            self.assertEqual(exit_code, 0, f"act did not complete successfully after resume:\n{output}")
            self.assertIn("step two", output)
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if container_name:
                runner.rm_container(engine, container_name)
            shutil.rmtree(repo_root, ignore_errors=True)
            shutil.rmtree(inject_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
