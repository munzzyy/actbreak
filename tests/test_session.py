"""Tests for actbreak.session: session-state round-trip, locate_workflow
resolution, and the run/resume cleanup paths -- all against fakes (a fake
Popen, and CommandRunner injected with a fake `run`), never a real
docker/podman/act/subprocess. The end-to-end path with the real tools is
covered separately by tests/test_integration.py."""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from actbreak import session
from actbreak.errors import SessionError
from actbreak.runtime import CommandRunner, Container

# Canned `ps --format {{.ID}}\t{{.Names}}\t{{.Status}}` output, same shape
# tests/test_runtime.py uses.
ONE_MATCH_PS = "c1\tact-CI-build\tUp 1 minute\n"
TWO_MATCH_PS = (
    "c1\tact-CI-build\tUp 1 minute\n"
    "c2\tact-CI2-build\tUp 1 minute\n"
)
NO_MATCH_PS = "c9\tunrelated-container\tUp 1 hour\n"


@dataclass
class FakeResult:
    stdout: str = ""
    returncode: int = 0


class FakeRunFn:
    """Records every call and returns pre-programmed results, keyed by a
    substring of the joined argv (first match wins). A `raises` pattern
    simulates a missing binary (FileNotFoundError from subprocess itself,
    as opposed to a nonzero exit -- `run(..., check=False)` never raises
    for that)."""

    def __init__(self, responses=None, raises=None, default=None):
        self.calls = []
        self.responses = responses or {}
        self.raises = raises or {}
        self.default = default if default is not None else FakeResult()

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        key = " ".join(args)
        for pattern, exc in self.raises.items():
            if pattern in key:
                raise exc
        for pattern, result in self.responses.items():
            if pattern in key:
                return result
        return self.default


class FakePopen:
    """Minimal stand-in for subprocess.Popen -- only the surface cmd_run and
    wait_for_breakpoint actually touch."""

    def __init__(self, running=True, exit_code=0):
        self.running = running
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return None if self.running else self.exit_code

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.running = False
        return self.exit_code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.running = False


def _no_interrupt():
    return None


@contextlib.contextmanager
def _patch_all(patchers):
    """Enter every context manager in `patchers` together, for a call site
    that needs a handful of mock.patch.object calls active at once."""
    with contextlib.ExitStack() as stack:
        for p in patchers:
            stack.enter_context(p)
        yield


# ---------------------------------------------------------------------------
# session state round-trip
# ---------------------------------------------------------------------------


class SessionStateRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state_dir = Path(self.tmp.name) / ".actbreak"
        patcher_dir = mock.patch.object(session, "STATE_DIR", state_dir)
        patcher_file = mock.patch.object(session, "STATE_FILE", state_dir / "state.json")
        patcher_dir.start()
        patcher_file.start()
        self.addCleanup(patcher_dir.stop)
        self.addCleanup(patcher_file.stop)

    def test_load_sessions_missing_file_returns_empty_list(self):
        self.assertEqual(session._load_sessions(), [])

    def test_save_then_load_sessions_round_trips(self):
        sessions = [
            {"container_id": "c1", "container_name": "act-CI-build", "runtime": "docker"},
            {"container_id": "c2", "container_name": "act-CI-test", "runtime": "podman"},
        ]
        session._save_sessions(sessions)
        self.assertTrue(session.STATE_FILE.is_file())
        self.assertEqual(session._load_sessions(), sessions)

    def test_load_sessions_corrupt_json_returns_empty_list_not_a_crash(self):
        session.STATE_DIR.mkdir(parents=True, exist_ok=True)
        session.STATE_FILE.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(session._load_sessions(), [])

    def test_load_sessions_non_list_sessions_key_returns_empty_list(self):
        session.STATE_DIR.mkdir(parents=True, exist_ok=True)
        session.STATE_FILE.write_text(json.dumps({"sessions": "not-a-list"}), encoding="utf-8")
        self.assertEqual(session._load_sessions(), [])


# ---------------------------------------------------------------------------
# locate_workflow / find_repo_root
# ---------------------------------------------------------------------------


class LocateWorkflowTests(unittest.TestCase):
    def _make_repo(self, tmp_path: Path, workflow_name: str = "ci.yml") -> Path:
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        wf = workflows_dir / workflow_name
        wf.write_text("name: CI\non: push\njobs:\n  build:\n    steps:\n      - run: echo hi\n")
        return wf

    def test_direct_path_resolves_repo_root_from_dot_github_ancestor(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            wf = self._make_repo(tmp_path)
            resolved, root = session.locate_workflow(str(wf))
            self.assertEqual(resolved, wf.resolve())
            self.assertEqual(root, tmp_path.resolve())

    def test_direct_path_outside_any_dot_github_falls_back_to_file_parent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            wf = tmp_path / "standalone.yml"
            wf.write_text("jobs:\n  build:\n    steps:\n      - run: echo hi\n")
            with mock.patch.object(session, "find_repo_root", return_value=None):
                resolved, root = session.locate_workflow(str(wf))
            self.assertEqual(resolved, wf.resolve())
            self.assertEqual(root, wf.resolve().parent)

    def test_bare_name_looked_up_under_dot_github_workflows(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            wf = self._make_repo(tmp_path, "smoke.yml")
            with mock.patch.object(Path, "cwd", return_value=tmp_path):
                resolved, root = session.locate_workflow("smoke")
            self.assertEqual(resolved, wf.resolve())
            self.assertEqual(root, tmp_path.resolve())

    def test_bare_name_tries_yml_and_yaml_suffixes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            wf = self._make_repo(tmp_path, "smoke.yaml")
            with mock.patch.object(Path, "cwd", return_value=tmp_path):
                resolved, _ = session.locate_workflow("smoke")
            self.assertEqual(resolved, wf.resolve())

    def test_bare_name_no_repo_root_raises_session_error(self):
        with mock.patch.object(session, "find_repo_root", return_value=None):
            with self.assertRaises(SessionError) as ctx:
                session.locate_workflow("nope")
        self.assertIn("no .github/workflows directory", str(ctx.exception))

    def test_bare_name_not_found_in_workflows_dir_raises_session_error(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            self._make_repo(tmp_path, "smoke.yml")
            with mock.patch.object(Path, "cwd", return_value=tmp_path):
                with self.assertRaises(SessionError) as ctx:
                    session.locate_workflow("does-not-exist")
            self.assertIn("not found", str(ctx.exception))

    def test_find_repo_root_walks_up_from_a_nested_directory(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            (tmp_path / ".github" / "workflows").mkdir(parents=True)
            nested = tmp_path / "src" / "deeply" / "nested"
            nested.mkdir(parents=True)
            self.assertEqual(session.find_repo_root(nested), tmp_path.resolve())

    def test_find_repo_root_returns_none_when_no_ancestor_has_dot_github(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(session.find_repo_root(Path(td)))


# ---------------------------------------------------------------------------
# wait_for_breakpoint
# ---------------------------------------------------------------------------


class WaitForBreakpointTests(unittest.TestCase):
    def test_ambiguous_containers_raise_immediately_not_swallowed(self):
        # Before the fix, find_job_container's "multiple matches" error was
        # caught by the same `except ContainerNotFoundError` that's meant
        # for "not found yet, keep polling" -- so this spun until timeout
        # instead of surfacing right away.
        runner = CommandRunner(run=FakeRunFn({"ps": FakeResult(stdout=TWO_MATCH_PS)}))
        proc = FakePopen(running=True)
        with self.assertRaises(SessionError) as ctx:
            session.wait_for_breakpoint(
                proc, runner, "docker", "build", None, _no_interrupt, timeout=5
            )
        self.assertIn("multiple containers match", str(ctx.exception))

    def test_hold_file_found_returns_the_container(self):
        runner = CommandRunner(
            run=FakeRunFn(
                {"ps": FakeResult(stdout=ONE_MATCH_PS), "test -f": FakeResult(returncode=0)}
            )
        )
        proc = FakePopen(running=True)
        container = session.wait_for_breakpoint(
            proc, runner, "docker", "build", None, _no_interrupt, timeout=5
        )
        self.assertEqual(container.name, "act-CI-build")

    def test_act_exiting_before_the_hold_returns_none(self):
        runner = CommandRunner(run=FakeRunFn({"ps": FakeResult(stdout=NO_MATCH_PS)}))
        proc = FakePopen(running=False, exit_code=1)
        result = session.wait_for_breakpoint(
            proc, runner, "docker", "build", None, _no_interrupt, timeout=5
        )
        self.assertIsNone(result)

    def test_timeout_raises_session_error(self):
        runner = CommandRunner(run=FakeRunFn({"ps": FakeResult(stdout=NO_MATCH_PS)}))
        proc = FakePopen(running=True)
        with self.assertRaises(SessionError) as ctx:
            # Already-elapsed deadline -- fires on the very first check,
            # no real sleeping needed for a deterministic test.
            session.wait_for_breakpoint(
                proc, runner, "docker", "build", None, _no_interrupt, timeout=-1
            )
        self.assertIn("timed out", str(ctx.exception))


# ---------------------------------------------------------------------------
# cmd_run cleanup wiring
# ---------------------------------------------------------------------------


def _run_args(**overrides):
    defaults = dict(
        workflow="ci.yml",
        break_before="Run tests",
        break_after=None,
        break_on_failure=False,
        job=None,
        runtime="auto",
        no_attach=False,
        act_arg=[],
        verbose=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class CmdRunCleanupTests(unittest.TestCase):
    """cmd_run wired to fakes at every external boundary (Popen, act/runtime
    detection, wait_for_breakpoint) so the exception-handling/cleanup logic
    itself -- the part A2 fixes -- runs for real."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name)
        workflows_dir = self.repo_root / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        self.workflow = workflows_dir / "ci.yml"
        self.workflow.write_text(
            "name: CI\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - name: Run tests\n        run: echo hi\n"
        )
        # A real directory (not wrapped in TemporaryDirectory) -- cmd_run's
        # own cleanup path is expected to rmtree this itself in most of
        # these tests, so ownership of removing it belongs to the code
        # under test, not to an auto-cleanup object that would double-remove it.
        self.injected_dir = tempfile.mkdtemp(prefix="actbreak-test-inject-")
        self.addCleanup(shutil.rmtree, self.injected_dir, True)

    def _patched(self, popen, fake_run, wait_side_effect):
        return (
            mock.patch.object(subprocess, "Popen", return_value=popen),
            mock.patch.object(session, "require_act", return_value="/usr/bin/act"),
            mock.patch.object(session, "detect_runtime", return_value="docker"),
            mock.patch.object(session, "wait_for_breakpoint", side_effect=wait_side_effect),
            mock.patch.object(session, "CommandRunner", lambda: CommandRunner(run=fake_run)),
            mock.patch.object(session.tempfile, "mkdtemp", return_value=self.injected_dir),
        )

    def test_session_error_during_wait_terminates_act_and_removes_container(self):
        popen = FakePopen(running=True)
        fake_run = FakeRunFn({"ps": FakeResult(stdout=ONE_MATCH_PS)})

        def raise_timeout(*a, **kw):
            raise SessionError("timed out after 5s waiting for job 'build' to hit the breakpoint")

        patchers = self._patched(popen, fake_run, raise_timeout)
        with _patch_all(patchers):
            args = _run_args(workflow=str(self.workflow))
            with self.assertRaises(SessionError):
                session.cmd_run(args)

        self.assertTrue(popen.terminated, "the orphaned act process must be terminated")
        rm_calls = [c for c in fake_run.calls if "rm" in c]
        self.assertTrue(rm_calls, f"expected a container rm call, got: {fake_run.calls}")
        self.assertIn("act-CI-build", rm_calls[0])
        self.assertFalse(
            Path(self.injected_dir).exists(), "the injection tmpdir must still be cleaned up"
        )

    def test_session_error_when_act_already_exited_does_not_call_terminate_again(self):
        # proc.poll() already non-None (act exited on its own) -- cleanup
        # must not blow up calling terminate() on a dead process, but it
        # must still run (the container search below proves that).
        popen = FakePopen(running=False, exit_code=1)
        fake_run = FakeRunFn({"ps": FakeResult(stdout=NO_MATCH_PS)})

        def raise_timeout(*a, **kw):
            raise SessionError("timed out")

        patchers = self._patched(popen, fake_run, raise_timeout)
        with _patch_all(patchers):
            args = _run_args(workflow=str(self.workflow))
            with self.assertRaises(SessionError):
                session.cmd_run(args)
        self.assertFalse(popen.terminated)
        ps_calls = [c for c in fake_run.calls if "ps" in c]
        self.assertTrue(ps_calls, "the cleanup path's container search must still run")

    def test_normal_breakpoint_hit_still_records_session_no_attach(self):
        # Regression guard: the new SessionError handling must not disturb
        # the existing successful --no-attach path.
        popen = FakePopen(running=True)
        fake_run = FakeRunFn(
            {"ps": FakeResult(stdout=ONE_MATCH_PS), "test -f": FakeResult(returncode=0)}
        )

        def fake_wait(*a, **kw):
            return Container(id="c1", name="act-CI-build")

        state_dir = Path(self.tmp.name) / ".actbreak"
        patchers = self._patched(popen, fake_run, fake_wait) + (
            mock.patch.object(session, "STATE_DIR", state_dir),
            mock.patch.object(session, "STATE_FILE", state_dir / "state.json"),
        )
        with _patch_all(patchers):
            args = _run_args(workflow=str(self.workflow), no_attach=True)
            rc = session.cmd_run(args)
        self.assertEqual(rc, 0)
        sessions = json.loads((state_dir / "state.json").read_text())["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["container_name"], "act-CI-build")


# ---------------------------------------------------------------------------
# cmd_resume
# ---------------------------------------------------------------------------


class CmdResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / ".actbreak"
        self.state_file = self.state_dir / "state.json"
        self._patchers = [
            mock.patch.object(session, "STATE_DIR", self.state_dir),
            mock.patch.object(session, "STATE_FILE", self.state_file),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def _seed(self, sessions):
        session._save_sessions(sessions)

    def test_resume_success_clears_the_session(self):
        self._seed(
            [{"runtime": "docker", "container_id": "c1", "container_name": "act-CI-build", "tmpdir": None}]
        )
        fake_run = FakeRunFn(default=FakeResult(returncode=0))
        with mock.patch.object(session, "CommandRunner", lambda: CommandRunner(run=fake_run)):
            rc = session.cmd_resume(None)
        self.assertEqual(rc, 0)
        self.assertEqual(session._load_sessions(), [])

    def test_resume_failure_retains_the_failed_session_entry(self):
        # "state says podman but it's uninstalled" -- subprocess.run raises
        # FileNotFoundError, not a nonzero exit (rm_file uses check=False,
        # so a nonzero exit alone would never raise).
        good = {"runtime": "docker", "container_id": "c1", "container_name": "act-CI-good", "tmpdir": None}
        bad = {"runtime": "podman", "container_id": "c2", "container_name": "act-CI-bad", "tmpdir": None}
        self._seed([good, bad])

        fake_run = FakeRunFn(
            default=FakeResult(returncode=0),
            raises={"podman": FileNotFoundError("[Errno 2] No such file or directory: 'podman'")},
        )
        with mock.patch.object(session, "CommandRunner", lambda: CommandRunner(run=fake_run)):
            rc = session.cmd_resume(None)

        self.assertEqual(rc, 1)
        remaining = session._load_sessions()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["container_id"], "c2")

    def test_no_sessions_returns_1_without_touching_state_file(self):
        rc = session.cmd_resume(None)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
