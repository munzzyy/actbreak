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
import re
import shutil
import signal
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
    The hold is released with a direct exec rm; CliFlowIntegrationTest
    covers `resume`."""

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


CLI_SMOKE_WORKFLOW = """\
name: actbreak cli smoke
on: push

jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - name: step one
        run: echo "cli step one"
      - name: step two
        run: echo "cli step two ran"
"""

NAMED_JOB_WORKFLOW = """\
name: Named job check
on: push

jobs:
  test:
    name: Unit suite
    runs-on: ubuntu-latest
    steps:
      - name: step one
        run: echo "named step one"
      - name: step two
        run: echo "named step two ran"
"""

FAILING_WORKFLOW = """\
name: actbreak cli failure
on: push

jobs:
  fail:
    runs-on: ubuntu-latest
    steps:
      - name: fails
        run: exit 3
"""

PIN_IMAGE = ("--act-arg=-P", "--act-arg=ubuntu-latest=catthehacker/ubuntu:act-latest")


@unittest.skipUnless(_tools_available(), SKIP_REASON)
class CliFlowIntegrationTest(unittest.TestCase):
    """`run --no-attach`, `list`, `resume` and `clean` as commands, the way a
    user types them, against what real act leaves behind: containers named
    after a job's own `name:`, and job containers that `act --reuse` keeps
    running after act exits."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="actbreak-it-cli-"))
        self.home = self.work / "home"
        self.home.mkdir()
        self.repo = self.work / "repo"
        (self.repo / ".github" / "workflows").mkdir(parents=True)
        source_root = str(Path(__file__).resolve().parent.parent)
        pythonpath = os.pathsep.join(filter(None, [source_root, os.environ.get("PYTHONPATH")]))
        self.env = dict(os.environ, HOME=str(self.home), PYTHONPATH=pythonpath)
        self.engine = detect_runtime("auto")
        self.runner = CommandRunner()
        self.calls = 0
        self.addCleanup(shutil.rmtree, self.work, True)

    def _workflow(self, filename, text, container_token):
        (self.repo / ".github" / "workflows" / filename).write_text(text, encoding="utf-8")
        self.addCleanup(self._remove_leftovers, container_token)

    def _remove_leftovers(self, container_token):
        for s in self._sessions():
            pid = s.get("act_pid")
            if isinstance(pid, int) and pid > 0:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        for name in self._container_names(container_token):
            self.runner.rm_container(self.engine, name)

    def _actbreak(self, *args, timeout=300):
        """Run the CLI with its output in a file of its own, never a pipe: a
        detached act keeps the stdout it inherited open after actbreak exits."""
        self.calls += 1
        out_path = self.work / f"{self.calls:02d}-{args[0]}.log"
        with open(out_path, "wb") as out:
            result = subprocess.run(
                [sys.executable, "-m", "actbreak", *args],
                cwd=self.repo, env=self.env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        return result.returncode, out_path

    def _read(self, path):
        return path.read_text(encoding="utf-8", errors="replace")

    def _sessions(self):
        state = self.home / ".actbreak" / "state.json"
        if not state.is_file():
            return []
        return json.loads(state.read_text(encoding="utf-8"))["sessions"]

    def _container_names(self, token):
        listing = subprocess.run(
            [self.engine, "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, check=True
        )
        return [n for n in listing.stdout.split() if n.startswith("act-") and token in n]

    def _park_then_resume(self, workflow, breakpoint, container_token):
        rc, run_log = self._actbreak(
            "run", workflow, "--break-before", breakpoint, "--no-attach", "--timeout", "240", *PIN_IMAGE
        )
        self.assertEqual(rc, 0, f"run failed:\n{self._read(run_log)[-4000:]}")
        self.assertIn("breakpoint hit", self._read(run_log))
        sessions = self._sessions()
        self.assertEqual(len(sessions), 1, sessions)
        self.assertIn(sessions[0]["container_name"], self._container_names(container_token))

        rc, list_log = self._actbreak("list")
        self.assertEqual(rc, 0, self._read(list_log))
        self.assertIn(f"{sessions[0]['container_name']} [running]", self._read(list_log))

        try:
            rc, resume_log = self._actbreak("resume", timeout=180)
        except subprocess.TimeoutExpired:
            self.fail(f"resume did not finish within 180 s; act output:\n{self._read(run_log)[-4000:]}")
        act_output = self._read(run_log)[-4000:]
        self.assertEqual(rc, 0, f"resume failed:\n{self._read(resume_log)}\nact output:\n{act_output}")
        self.assertEqual(self._container_names(container_token), [], self._read(resume_log))
        self.assertEqual(self._sessions(), [])
        return run_log

    def test_run_list_resume_reaps_the_job(self):
        self._workflow("smoke.yml", CLI_SMOKE_WORKFLOW, "cli-smoke")
        run_log = self._park_then_resume("smoke.yml", "step two", "cli-smoke")
        self.assertIn("cli step two ran", self._read(run_log))

    def test_a_job_with_its_own_name_is_found_and_reaped(self):
        self._workflow("named.yml", NAMED_JOB_WORKFLOW, "Named-job-check")
        run_log = self._park_then_resume("named.yml", "test:1", "Named-job-check")
        self.assertIn("Unit-suite", self._read(run_log))
        self.assertIn("named step two ran", self._read(run_log))

    def test_break_on_failure_parks_a_container_you_can_exec_into_and_clean(self):
        self._workflow("fail.yml", FAILING_WORKFLOW, "cli-failure")
        rc, run_log = self._actbreak("run", "fail.yml", "--break-on-failure", "--no-attach", *PIN_IMAGE)
        output = self._read(run_log)
        # act exits 1 for any failed job, whatever the step's own exit code was.
        self.assertNotEqual(rc, 0, output[-4000:])
        self.assertIn(f"act exited {rc}", output)
        found = re.search(r"post-mortem container: (\S+)", output)
        self.assertIsNotNone(found, output[-4000:])
        name = found.group(1)
        self.assertEqual(subprocess.run([self.engine, "exec", name, "true"]).returncode, 0)

        rc, clean_log = self._actbreak("clean")
        self.assertEqual(rc, 0, self._read(clean_log))
        self.assertIn(f"cleaned {name}", self._read(clean_log))
        self.assertEqual(self._container_names("cli-failure"), [])
        self.assertEqual(self._sessions(), [])


if __name__ == "__main__":
    unittest.main()
