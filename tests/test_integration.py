"""End-to-end integration tests: real act, real docker/podman, no fakes.

Skipped automatically wherever `act` and a container runtime aren't both on
PATH -- which is everywhere except the dedicated `integration` job in CI
(see .github/workflows/ci.yml). These are the only tests in the suite that
touch real infrastructure; everything else in tests/ is a pure unit test.

Written as a plain unittest.TestCase so `python -m unittest` can always
import and skip it, even when pytest itself isn't installed. The
`pytestmark` below is set conditionally so pytest can still select it with
`-m integration`, without making pytest a hard import-time dependency.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
                # -P pins the runner image explicitly. Without it, a fresh act
                # install pops an interactive image-picker on first run and
                # dies with "fatal msg=EOF" on CI's non-tty stdin.
                [
                    act_bin,
                    "-W", inject_dir,
                    "-j", "smoke",
                    "--reuse",
                    "-P", "ubuntu-latest=catthehacker/ubuntu:act-latest",
                ],
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
            if container is None:
                # act exited instead of holding. Its output is the only clue,
                # so put it in the failure instead of swallowing it.
                out = b""
                try:
                    out = proc.stdout.read() or b""
                except Exception:
                    pass
                tail = out.decode("utf-8", "replace")[-4000:]
                self.fail(
                    "act exited before the breakpoint was ever reached; "
                    f"exit code {proc.returncode}, output tail:\n{tail}"
                )
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


MATRIX_WORKFLOW = """\
name: actbreak matrix check
on: push

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        n: [1, 2]
    steps:
      - name: step one
        run: echo "leg ${{ matrix.n }} started"
      - name: step two
        run: echo "step two ran on leg ${{ matrix.n }}"
"""


@unittest.skipUnless(_tools_available(), SKIP_REASON)
class MatrixLegIntegrationTest(unittest.TestCase):
    """Drives the real CLI: `run --matrix ... --no-attach`, then `clean`.

    The hold is released with a direct exec rm rather than `actbreak resume`,
    because resume's wait for the job to finish depends on the container
    state `act --reuse` leaves behind, which nothing here pins down yet."""

    def test_matrix_flag_runs_one_leg_to_the_breakpoint(self):
        work = Path(tempfile.mkdtemp(prefix="actbreak-it-matrix-"))
        home = work / "home"
        home.mkdir()
        repo = work / "repo"
        workflows_dir = repo / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "matrix.yml").write_text(MATRIX_WORKFLOW, encoding="utf-8")
        log = work / "actbreak.log"
        source_root = str(Path(__file__).resolve().parent.parent)
        pythonpath = os.pathsep.join(filter(None, [source_root, os.environ.get("PYTHONPATH")]))
        env = dict(os.environ, HOME=str(home), PYTHONPATH=pythonpath)
        engine = detect_runtime("auto")
        runner = CommandRunner()

        def actbreak(*args, timeout):
            # A file, not a pipe: the detached act keeps this stdout open after actbreak exits.
            with open(log, "ab") as out:
                return subprocess.run(
                    [sys.executable, "-m", "actbreak", *args],
                    cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                    timeout=timeout,
                )

        def leg_containers():
            return [c for c in runner.ps(engine, all_containers=True)
                    if c.name.startswith("act-") and "matrix-check" in c.name.lower()]

        try:
            result = actbreak(
                "run", "matrix.yml", "--break-before", "step two", "--matrix", "n:1", "--no-attach",
                "--timeout", "300", "--act-arg=-P", "--act-arg=ubuntu-latest=catthehacker/ubuntu:act-latest",
                timeout=420,
            )
            output = log.read_text(encoding="utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, f"run failed, output tail:\n{output[-4000:]}")
            self.assertIn("breakpoint hit", output)

            sessions = json.loads((home / ".actbreak" / "state.json").read_text(encoding="utf-8"))["sessions"]
            self.assertEqual(len(sessions), 1, sessions)
            parked = sessions[0]
            self.assertEqual([c.name for c in leg_containers()], [parked["container_name"]])

            self.assertTrue(runner.rm_file(engine, parked["container_id"], "/tmp/actbreak/hold"))
            deadline = time.monotonic() + 180
            while "step two ran on leg 1" not in log.read_text(encoding="utf-8", errors="replace"):
                if time.monotonic() > deadline:
                    tail = log.read_text(encoding="utf-8", errors="replace")[-4000:]
                    self.fail(f"the job never got past the released breakpoint, output tail:\n{tail}")
                time.sleep(2)

            cleaned = actbreak("clean", timeout=120)
            self.assertEqual(cleaned.returncode, 0, log.read_text(encoding="utf-8", errors="replace")[-2000:])
            self.assertEqual(leg_containers(), [])
            sessions = json.loads((home / ".actbreak" / "state.json").read_text(encoding="utf-8"))["sessions"]
            self.assertEqual(sessions, [])
        finally:
            for c in leg_containers():
                runner.rm_container(engine, c.name)
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
