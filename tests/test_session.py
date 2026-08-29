"""Tests for actbreak.session: session-state round-trip, locate_workflow
resolution, and the run/resume cleanup paths -- all against fakes (a fake
Popen, and CommandRunner injected with a fake `run`), never a real
docker/podman/act/subprocess. The end-to-end path with the real tools is
covered separately by tests/test_integration.py."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from actbreak import injector, session
from actbreak.errors import SelectorError, SessionError
from actbreak.runtime import CommandRunner, Container
from actbreak.selector import resolve_selector

from .util import fixture_path

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

    def test_matrix_ambiguity_spells_out_the_attach_commands(self):
        # runtime.py names the legs but has no idea which engine is in use,
        # so the runnable command has to be added here.
        matrix_ps = (
            "aaaaaaaaaaaa\tact-Matrix-CI-test-3.10-ubuntu-latest\tUp 1 minute\n"
            "bbbbbbbbbbbb\tact-Matrix-CI-test-3.11-ubuntu-latest\tUp 1 minute\n"
        )
        runner = CommandRunner(run=FakeRunFn({"ps": FakeResult(stdout=matrix_ps)}))
        proc = FakePopen(running=True)
        with self.assertRaises(SessionError) as ctx:
            session.wait_for_breakpoint(
                proc, runner, "podman", "test", "Matrix CI", _no_interrupt, timeout=5
            )
        message = str(ctx.exception)
        self.assertIn("matrix", message)
        self.assertIn("podman exec -it act-Matrix-CI-test-3.10-ubuntu-latest sh", message)
        self.assertIn("podman exec -it act-Matrix-CI-test-3.11-ubuntu-latest sh", message)

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
        breakpoints=[("before", "Run tests")],
        break_on_failure=False,
        job=None,
        runtime="auto",
        no_attach=False,
        shell=None,
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

    def test_clean_completion_reaps_the_reuse_container(self):
        # Breakpoint hit, attached, resumed, job runs to the end. --reuse
        # left the container behind; a clean run must reap it, not leak it.
        popen = FakePopen(running=True, exit_code=0)
        fake_run = FakeRunFn(
            {"ps": FakeResult(stdout=ONE_MATCH_PS), "test -f": FakeResult(returncode=0)}
        )

        def fake_wait(*a, **kw):
            return Container(id="c1", name="act-CI-build")

        patchers = self._patched(popen, fake_run, fake_wait)
        with _patch_all(patchers):
            args = _run_args(workflow=str(self.workflow))
            rc = session.cmd_run(args)
        self.assertEqual(rc, 0)
        container_rm = [c for c in fake_run.calls if c[:2] == ["docker", "rm"]]
        self.assertTrue(
            container_rm, f"a clean run must reap the --reuse container, got: {fake_run.calls}"
        )
        self.assertIn("act-CI-build", container_rm[0])

    def test_multiple_breakpoints_step_through_in_one_run(self):
        # Two breakpoints ("before" and "after" the same step, so a single
        # existing fixture step is enough) must both be waited on, attached
        # to, and resumed in one cmd_run call -- not just the first one.
        popen = FakePopen(running=True, exit_code=0)
        fake_run = FakeRunFn(
            {"ps": FakeResult(stdout=ONE_MATCH_PS), "test -f": FakeResult(returncode=0)}
        )
        hits = [Container(id="c1", name="act-CI-build"), Container(id="c1", name="act-CI-build")]

        def fake_wait(*a, **kw):
            return hits.pop(0)

        patchers = self._patched(popen, fake_run, fake_wait)
        with _patch_all(patchers):
            args = _run_args(
                workflow=str(self.workflow),
                breakpoints=[("before", "Run tests"), ("after", "Run tests")],
            )
            rc = session.cmd_run(args)
        self.assertEqual(rc, 0)
        self.assertEqual(hits, [], "wait_for_breakpoint must be called once per breakpoint")
        attach_calls = [c for c in fake_run.calls if "-it" in c]
        self.assertEqual(len(attach_calls), 2, f"expected an attach per breakpoint, got: {fake_run.calls}")

    def test_no_attach_with_multiple_breakpoints_parks_at_the_first_with_the_rest_pending(self):
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
            args = _run_args(
                workflow=str(self.workflow),
                breakpoints=[("before", "Run tests"), ("after", "Run tests")],
                no_attach=True,
            )
            rc = session.cmd_run(args)
        self.assertEqual(rc, 0)
        sessions = json.loads((state_dir / "state.json").read_text())["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["label"], "Run tests")
        self.assertEqual(sessions[0]["position"], "before")
        self.assertEqual(sessions[0]["pending"], [["Run tests", "after"]])
        # Only the first breakpoint was ever waited for -- --no-attach stops
        # there, exactly like a single-breakpoint run always has.
        attach_calls = [c for c in fake_run.calls if "-it" in c]
        self.assertEqual(attach_calls, [])

    def test_breakpoints_spanning_two_jobs_are_rejected(self):
        # actbreak run scopes a single act invocation to one job (`-j`); a
        # breakpoint sequence that resolves to more than one job can never
        # actually be reached, so it must fail fast with a clear error
        # instead of quietly debugging the wrong job.
        multi = self.repo_root / ".github" / "workflows" / "multi.yml"
        multi.write_text(
            "name: Build and Test\non: push\njobs:\n"
            "  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - name: Lint\n        run: ruff check .\n"
            "  build:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - name: Upload artifact\n        run: echo up\n"
        )
        args = _run_args(
            workflow=str(multi),
            breakpoints=[("before", "Lint"), ("before", "Upload artifact")],
        )
        with self.assertRaises(SelectorError):
            session.cmd_run(args)

    def test_break_on_failure_success_reaps_container_without_a_job_flag(self):
        # The exact command from the field report: `actbreak run ci.yml
        # --break-on-failure` with no -j. The job passes, so no post-mortem
        # fires -- the reuse container still has to be reaped by workflow name.
        popen = FakePopen(running=False, exit_code=0)
        fake_run = FakeRunFn({"ps": FakeResult(stdout=ONE_MATCH_PS)})
        patchers = self._patched(popen, fake_run, lambda *a, **k: None)
        with _patch_all(patchers):
            args = _run_args(
                workflow=str(self.workflow),
                breakpoints=[],
                break_on_failure=True,
                job=None,
            )
            rc = session.cmd_run(args)
        self.assertEqual(rc, 0)
        container_rm = [c for c in fake_run.calls if c[:2] == ["docker", "rm"]]
        self.assertTrue(
            container_rm, f"a passing --break-on-failure run must reap the container, got: {fake_run.calls}"
        )
        self.assertIn("act-CI-build", container_rm[0])

    def test_break_on_failure_reap_never_touches_an_unrelated_container(self):
        # Field report: a parked debug container from ANOTHER workflow
        # (act-Other-Workflow-otherjob) must never be reaped by a passing
        # `--break-on-failure` run whose own job produced no container.
        popen = FakePopen(running=False, exit_code=0)
        fake_run = FakeRunFn({"ps": FakeResult(stdout="cx\tact-Other-Workflow-otherjob\tExited (0)\n")})
        patchers = self._patched(popen, fake_run, lambda *a, **k: None)
        with _patch_all(patchers):
            args = _run_args(
                workflow=str(self.workflow), breakpoints=[], break_on_failure=True, job=None
            )
            rc = session.cmd_run(args)
        self.assertEqual(rc, 0)
        container_rm = [c for c in fake_run.calls if c[:2] == ["docker", "rm"]]
        self.assertFalse(
            container_rm, f"an unrelated workflow's container must not be reaped, got: {fake_run.calls}"
        )

    def test_break_on_failure_postmortem_never_attaches_to_an_unrelated_container(self):
        # The nastier half: act FAILS, and the only act-* container around is
        # someone else's parked debug session. The post-mortem must not exec
        # into or force-remove it, and must not present it as this run's.
        popen = FakePopen(running=False, exit_code=1)
        fake_run = FakeRunFn({"ps": FakeResult(stdout="cx\tact-Other-Workflow-otherjob\tExited (1)\n")})
        patchers = self._patched(popen, fake_run, lambda *a, **k: None)
        with _patch_all(patchers):
            args = _run_args(
                workflow=str(self.workflow), breakpoints=[], break_on_failure=True, job=None
            )
            rc = session.cmd_run(args)
        self.assertEqual(rc, 1)
        attach = [c for c in fake_run.calls if "-it" in c]
        rm = [c for c in fake_run.calls if c[:2] == ["docker", "rm"]]
        self.assertFalse(attach, f"post-mortem must not exec into an unrelated container, got: {fake_run.calls}")
        self.assertFalse(rm, f"post-mortem must not remove an unrelated container, got: {fake_run.calls}")

    def test_passing_break_on_failure_reaps_every_job_container_in_a_multijob_workflow(self):
        # README promises a passing --break-on-failure run leaves nothing
        # behind. With two jobs, --reuse leaves TWO containers; both must be
        # reaped, not just a single global match.
        two = self.repo_root / ".github" / "workflows" / "two.yml"
        two.write_text(
            "name: CI\non: push\njobs:\n"
            "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - name: A\n        run: echo hi\n"
            "  test:\n    runs-on: ubuntu-latest\n    steps:\n      - name: B\n        run: echo hi\n"
        )
        popen = FakePopen(running=False, exit_code=0)
        fake_run = FakeRunFn(
            {"ps": FakeResult(stdout="c1\tact-CI-build\tExited (0)\nc2\tact-CI-test\tExited (0)\n")}
        )
        patchers = self._patched(popen, fake_run, lambda *a, **k: None)
        with _patch_all(patchers):
            args = _run_args(workflow=str(two), breakpoints=[], break_on_failure=True, job=None)
            rc = session.cmd_run(args)
        self.assertEqual(rc, 0)
        removed = {c[-1] for c in fake_run.calls if c[:2] == ["docker", "rm"]}
        self.assertEqual(
            removed, {"act-CI-build", "act-CI-test"},
            f"both job containers must be reaped, got: {fake_run.calls}",
        )

    def test_interrupt_during_break_on_failure_reaps_the_container_without_a_job_flag(self):
        # Ctrl+C during `--break-on-failure` (no -j): act is killed, and its
        # container -- identified by workflow name, never a blind act-* grab --
        # must be removed rather than left running.
        class InterruptingPopen(FakePopen):
            def __init__(self):
                super().__init__(running=True, exit_code=0)
                self._waits = 0

            def wait(self, timeout=None):
                self._waits += 1
                if self._waits == 1:
                    raise session._Interrupted()
                self.running = False
                return self.exit_code

        popen = InterruptingPopen()
        fake_run = FakeRunFn({"ps": FakeResult(stdout="c1\tact-CI-build\tExited (0)\n")})
        patchers = self._patched(popen, fake_run, lambda *a, **k: None)
        with _patch_all(patchers):
            args = _run_args(
                workflow=str(self.workflow), breakpoints=[], break_on_failure=True, job=None
            )
            rc = session.cmd_run(args)
        self.assertEqual(rc, 130)
        self.assertTrue(popen.terminated, "act must be terminated on interrupt")
        container_rm = [c for c in fake_run.calls if c[:2] == ["docker", "rm"]]
        self.assertTrue(
            container_rm, f"the interrupted run's container must be reaped, got: {fake_run.calls}"
        )
        self.assertIn("act-CI-build", container_rm[0])

    def test_no_attach_hold_does_not_reap_the_container(self):
        # Regression guard: the intentionally-held --no-attach container must
        # survive so `actbreak resume` can still reach it.
        popen = FakePopen(running=True)
        fake_run = FakeRunFn(
            {"ps": FakeResult(stdout=ONE_MATCH_PS), "test -f": FakeResult(returncode=0)}
        )

        def fake_wait(*a, **kw):
            return Container(id="c1", name="act-CI-build")

        state_dir = Path(self.tmp.name) / ".actbreak-hold"
        patchers = self._patched(popen, fake_run, fake_wait) + (
            mock.patch.object(session, "STATE_DIR", state_dir),
            mock.patch.object(session, "STATE_FILE", state_dir / "state.json"),
        )
        with _patch_all(patchers):
            args = _run_args(workflow=str(self.workflow), no_attach=True)
            session.cmd_run(args)
        container_rm = [c for c in fake_run.calls if c[:2] == ["docker", "rm"]]
        self.assertFalse(
            container_rm, f"a held --no-attach container must not be reaped, got: {fake_run.calls}"
        )


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

    def test_resume_reaps_the_finished_container_so_it_is_not_leaked(self):
        # After the hold is dropped, the job runs to the end and --reuse leaves
        # the container stopped. resume must reap it by id (which works on a
        # stopped container), not just drop the record and orphan it.
        self._seed(
            [{"runtime": "docker", "container_id": "c1", "container_name": "act-CI-build", "tmpdir": None}]
        )
        fake_run = FakeRunFn(
            {"ps": FakeResult(stdout="c1\tact-CI-build\tExited (0)\n")},
            default=FakeResult(returncode=0),
        )
        with mock.patch.object(session, "CommandRunner", lambda: CommandRunner(run=fake_run)):
            rc = session.cmd_resume(None)
        self.assertEqual(rc, 0)
        container_rm = [c for c in fake_run.calls if c[:2] == ["docker", "rm"]]
        self.assertTrue(
            container_rm, f"resume must reap the finished container, got: {fake_run.calls}"
        )
        self.assertIn("c1", container_rm[0])
        self.assertEqual(session._load_sessions(), [])

    def test_resume_against_a_stopped_container_keeps_the_session_and_fails(self):
        # Post-reboot: the container is already stopped, so `exec rm` of the
        # hold fails (nonzero). resume must NOT report success and drop the
        # record -- it must keep it (so `clean` can reap it by id) and exit 1.
        self._seed(
            [{"runtime": "docker", "container_id": "c1", "container_name": "act-CI-build", "tmpdir": None}]
        )
        fake_run = FakeRunFn({"exec": FakeResult(returncode=1)}, default=FakeResult(returncode=0))
        with mock.patch.object(session, "CommandRunner", lambda: CommandRunner(run=fake_run)):
            rc = session.cmd_resume(None)
        self.assertEqual(rc, 1)
        remaining = session._load_sessions()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["container_id"], "c1")

    def test_progress_is_saved_after_each_session_not_only_at_the_end(self):
        # If the process dies while blocked resolving session N (a SIGTERM,
        # say, which skips straight past every except/finally in cmd_resume),
        # sessions 1..N-1 that already finished must already be off disk --
        # not still listed, which used to make a later resume try (and fail)
        # to resume an already-gone container.
        first = {"runtime": "docker", "container_id": "c1", "container_name": "act-CI-a", "tmpdir": None}
        second = {"runtime": "docker", "container_id": "c2", "container_name": "act-CI-b", "tmpdir": None}
        self._seed([first, second])
        fake_run = FakeRunFn(default=FakeResult(returncode=0))

        calls = {"n": 0}

        def fake_wait_and_reap(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return True  # session 1 finishes cleanly
            raise RuntimeError("killed mid-wait")  # simulates an external kill on session 2

        with mock.patch.object(session, "CommandRunner", lambda: CommandRunner(run=fake_run)):
            with mock.patch.object(session, "_wait_and_reap", side_effect=fake_wait_and_reap):
                with self.assertRaises(RuntimeError):
                    session.cmd_resume(None)

        # Session 1 must already be gone from disk even though the whole
        # call never returned normally.
        remaining_ids = [s["container_id"] for s in session._load_sessions()]
        self.assertNotIn("c1", remaining_ids)
        self.assertIn("c2", remaining_ids)

    def test_resume_hits_the_next_breakpoint_and_reparks_instead_of_finishing(self):
        # A session recorded by a multi-breakpoint run carries the ones still
        # ahead; if the job reaches the next hold before finishing, resume
        # must re-park at it rather than declare the job done.
        self._seed(
            [
                {
                    "runtime": "docker",
                    "container_id": "c1",
                    "container_name": "act-CI-build",
                    "tmpdir": None,
                    "label": "Install deps",
                    "position": "before",
                    "pending": [["Run tests", "after"]],
                }
            ]
        )
        fake_run = FakeRunFn(default=FakeResult(returncode=0))
        buf = io.StringIO()
        with mock.patch.object(session, "CommandRunner", lambda: CommandRunner(run=fake_run)):
            with mock.patch.object(session, "_wait_and_reap", return_value="hit"):
                with contextlib.redirect_stdout(buf):
                    rc = session.cmd_resume(None)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("breakpoint hit -- step 'Run tests' (after)", out)
        remaining = session._load_sessions()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["label"], "Run tests")
        self.assertEqual(remaining[0]["position"], "after")
        self.assertEqual(remaining[0]["pending"], [])
        # It's still held, not reaped.
        self.assertEqual([c for c in fake_run.calls if c[:2] == ["docker", "rm"]], [])

    def test_wait_and_reap_gives_up_on_a_container_that_never_stops(self):
        # The wait is bounded so `resume` can't block forever on a job that
        # hangs; giving up returns False and the caller keeps the session.
        fake_run = FakeRunFn(
            {"ps": FakeResult(stdout="c1\tact-CI-build\tUp 4 minutes\n")},
            default=FakeResult(returncode=0),
        )
        runner = CommandRunner(run=fake_run)
        with mock.patch.object(session, "POLL_INTERVAL", 0):
            result = session._wait_and_reap(runner, "docker", "c1", timeout=0)
        self.assertFalse(result)
        self.assertEqual([c for c in fake_run.calls if c[:2] == ["docker", "rm"]], [])

    def test_wait_and_reap_treats_an_already_removed_container_as_done(self):
        fake_run = FakeRunFn({"ps": FakeResult(stdout="")}, default=FakeResult(returncode=0))
        runner = CommandRunner(run=fake_run)
        with mock.patch.object(session, "POLL_INTERVAL", 0):
            result = session._wait_and_reap(runner, "docker", "c1", timeout=0)
        self.assertTrue(result)

    def test_wait_and_reap_reports_hit_when_the_next_hold_appears(self):
        # Container is still "Up" (the job didn't finish) but the hold file
        # is back -- it reached the next breakpoint in the sequence.
        fake_run = FakeRunFn(
            {"ps": FakeResult(stdout="c1\tact-CI-build\tUp 1 minute\n"), "test -f": FakeResult(returncode=0)}
        )
        runner = CommandRunner(run=fake_run)
        result = session._wait_and_reap(runner, "docker", "c1", timeout=5, pending=[("next", "before")])
        self.assertEqual(result, "hit")

    def test_wait_and_reap_ignores_the_hold_file_when_nothing_is_pending(self):
        # Regression guard: without `pending`, behavior must be exactly the
        # old one -- never even check for the hold file.
        fake_run = FakeRunFn(
            {"ps": FakeResult(stdout="c1\tact-CI-build\tUp 1 minute\n"), "test -f": FakeResult(returncode=0)}
        )
        runner = CommandRunner(run=fake_run)
        with mock.patch.object(session, "POLL_INTERVAL", 0):
            result = session._wait_and_reap(runner, "docker", "c1", timeout=0)
        self.assertFalse(result)
        self.assertFalse(any("test" in c for c in fake_run.calls))

    def test_resume_says_it_is_waiting_before_it_blocks(self):
        # Without this line the command prints "resumed X" and then sits
        # silent for as long as the rest of the workflow takes, which reads
        # as a hang.
        self._seed(
            [{"runtime": "docker", "container_id": "c1", "container_name": "act-CI-build", "tmpdir": None}]
        )
        fake_run = FakeRunFn(
            {"ps": FakeResult(stdout="c1\tact-CI-build\tExited (0)\n")},
            default=FakeResult(returncode=0),
        )
        buf = io.StringIO()
        with mock.patch.object(session, "CommandRunner", lambda: CommandRunner(run=fake_run)):
            with contextlib.redirect_stdout(buf):
                rc = session.cmd_resume(None)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("waiting for act-CI-build to finish", out)
        self.assertIn("Ctrl-C", out)

    def test_ctrl_c_while_waiting_keeps_every_remaining_session(self):
        # Ctrl-C means "stop watching", not "abort the job". The container is
        # still running, so its record has to survive for `clean`.
        first = {"runtime": "docker", "container_id": "c1", "container_name": "act-CI-a", "tmpdir": None}
        second = {"runtime": "docker", "container_id": "c2", "container_name": "act-CI-b", "tmpdir": None}
        self._seed([first, second])
        fake_run = FakeRunFn(default=FakeResult(returncode=0))
        buf = io.StringIO()
        with mock.patch.object(session, "CommandRunner", lambda: CommandRunner(run=fake_run)):
            with mock.patch.object(session, "_wait_and_reap", side_effect=KeyboardInterrupt):
                with contextlib.redirect_stdout(buf):
                    rc = session.cmd_resume(None)
        self.assertEqual(rc, 0)
        self.assertEqual([s["container_id"] for s in session._load_sessions()], ["c1", "c2"])

    def test_clean_does_not_report_cleaned_when_removal_fails(self):
        self._seed(
            [{"runtime": "docker", "container_id": "c1", "container_name": "act-CI-build", "tmpdir": None}]
        )
        fake_run = FakeRunFn(default=FakeResult(returncode=1))
        buf = io.StringIO()
        with mock.patch.object(session, "CommandRunner", lambda: CommandRunner(run=fake_run)):
            with mock.patch.object(session.shutil, "which", return_value=None):
                with contextlib.redirect_stdout(buf):
                    rc = session.cmd_clean(None)
        self.assertEqual(rc, 0)
        self.assertNotIn("cleaned", buf.getvalue())


# ---------------------------------------------------------------------------
# _session_age
# ---------------------------------------------------------------------------


class SessionAgeTests(unittest.TestCase):
    def test_none_for_missing_timestamp(self):
        self.assertIsNone(session._session_age(None))
        self.assertIsNone(session._session_age(""))

    def test_none_for_unparseable_timestamp(self):
        self.assertIsNone(session._session_age("not-a-timestamp"))

    def test_minutes_under_an_hour(self):
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=42)).isoformat()
        self.assertEqual(session._session_age(created_at), "42m")

    def test_hours_at_and_past_sixty_minutes(self):
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
        self.assertEqual(session._session_age(created_at), "1h")

    def test_naive_timestamp_treated_as_utc(self):
        # created_at is always written with datetime.now(timezone.utc), but a
        # stray naive timestamp must not raise trying to subtract it from an
        # aware one.
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None).isoformat()
        self.assertEqual(session._session_age(created_at), "10m")


# ---------------------------------------------------------------------------
# cmd_list
# ---------------------------------------------------------------------------


class CmdListTests(unittest.TestCase):
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

    def _run_list(self, fake_run):
        buf = io.StringIO()
        with mock.patch.object(session, "CommandRunner", lambda: CommandRunner(run=fake_run)):
            with contextlib.redirect_stdout(buf):
                rc = session.cmd_list(None)
        return rc, buf.getvalue()

    def test_no_sessions_prints_a_clean_message_and_lists_nothing(self):
        fake_run = FakeRunFn()
        rc, out = self._run_list(fake_run)
        self.assertEqual(rc, 0)
        self.assertIn("no parked debug sessions", out)
        # Nothing to inspect means no container listing was ever run.
        self.assertEqual(fake_run.calls, [])

    def test_running_container_is_reported_running(self):
        self._seed(
            [
                {
                    "runtime": "docker",
                    "container_id": "c1",
                    "container_name": "act-CI-build",
                    "job": "build",
                    "label": "Run tests",
                    "position": "before",
                    "workflow": "/repo/.github/workflows/ci.yml",
                    "tmpdir": None,
                }
            ]
        )
        fake_run = FakeRunFn({"ps": FakeResult(stdout=ONE_MATCH_PS)})
        rc, out = self._run_list(fake_run)
        self.assertEqual(rc, 0)
        self.assertIn("1 parked debug session:", out)
        self.assertIn("act-CI-build [running]", out)
        self.assertIn("job 'build', step 'Run tests' (before)", out)
        self.assertIn("/repo/.github/workflows/ci.yml", out)

    def test_stopped_container_is_reported_stopped(self):
        self._seed(
            [{"runtime": "docker", "container_id": "c1", "container_name": "act-CI-build", "tmpdir": None}]
        )
        fake_run = FakeRunFn({"ps": FakeResult(stdout="c1\tact-CI-build\tExited (0) 2 minutes ago\n")})
        rc, out = self._run_list(fake_run)
        self.assertEqual(rc, 0)
        self.assertIn("act-CI-build [stopped]", out)

    def test_missing_container_is_reported_gone(self):
        self._seed(
            [{"runtime": "docker", "container_id": "c1", "container_name": "act-CI-build", "tmpdir": None}]
        )
        # The listing has some other container, but not id c1 -- ours is gone.
        fake_run = FakeRunFn({"ps": FakeResult(stdout=NO_MATCH_PS)})
        rc, out = self._run_list(fake_run)
        self.assertEqual(rc, 0)
        self.assertIn("act-CI-build [gone]", out)

    def test_broken_runtime_is_reported_unknown_not_a_crash(self):
        # State names podman but it's since been uninstalled: ps raises
        # FileNotFoundError. The list must degrade to 'unknown', not blow up.
        self._seed(
            [{"runtime": "podman", "container_id": "c1", "container_name": "act-CI-build", "tmpdir": None}]
        )
        fake_run = FakeRunFn(raises={"podman": FileNotFoundError("no podman")})
        rc, out = self._run_list(fake_run)
        self.assertEqual(rc, 0)
        self.assertIn("act-CI-build [unknown]", out)

    def test_multiple_sessions_on_one_runtime_only_list_containers_once(self):
        self._seed(
            [
                {"runtime": "docker", "container_id": "c1", "container_name": "act-CI-build", "tmpdir": None},
                {"runtime": "docker", "container_id": "c2", "container_name": "act-CI2-build", "tmpdir": None},
            ]
        )
        fake_run = FakeRunFn({"ps": FakeResult(stdout=TWO_MATCH_PS)})
        rc, out = self._run_list(fake_run)
        self.assertEqual(rc, 0)
        self.assertIn("2 parked debug sessions:", out)
        self.assertIn("act-CI-build [running]", out)
        self.assertIn("act-CI2-build [running]", out)
        ps_calls = [c for c in fake_run.calls if "ps" in c]
        self.assertEqual(len(ps_calls), 1, f"the per-engine listing must be cached, got: {fake_run.calls}")

    def test_session_age_shown_in_minutes_under_an_hour(self):
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self._seed(
            [
                {
                    "runtime": "docker",
                    "container_id": "c1",
                    "container_name": "act-CI-build",
                    "tmpdir": None,
                    "created_at": created_at,
                }
            ]
        )
        fake_run = FakeRunFn({"ps": FakeResult(stdout=ONE_MATCH_PS)})
        rc, out = self._run_list(fake_run)
        self.assertEqual(rc, 0)
        self.assertIn("held for 5m", out)

    def test_session_age_shown_in_hours_past_sixty_minutes(self):
        created_at = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        self._seed(
            [{"runtime": "docker", "container_id": "c1", "container_name": "act-CI-build",
              "tmpdir": None, "created_at": created_at}]
        )
        fake_run = FakeRunFn({"ps": FakeResult(stdout=ONE_MATCH_PS)})
        rc, out = self._run_list(fake_run)
        self.assertIn("held for 5h", out)

    def test_missing_created_at_omits_the_age_entirely(self):
        # An older state file (or one from `clean`'s stray-container sweep,
        # which never records one) shouldn't print a bogus age.
        self._seed(
            [{"runtime": "docker", "container_id": "c1", "container_name": "act-CI-build", "tmpdir": None}]
        )
        fake_run = FakeRunFn({"ps": FakeResult(stdout=ONE_MATCH_PS)})
        rc, out = self._run_list(fake_run)
        self.assertNotIn("held for", out)

    def test_list_never_removes_a_container(self):
        # list is read-only -- it inspects status, it must never rm anything.
        self._seed(
            [{"runtime": "docker", "container_id": "c1", "container_name": "act-CI-build", "tmpdir": None}]
        )
        fake_run = FakeRunFn({"ps": FakeResult(stdout="c1\tact-CI-build\tExited (0)\n")})
        self._run_list(fake_run)
        rm_calls = [c for c in fake_run.calls if "rm" in c]
        self.assertEqual(rm_calls, [], f"list must not remove anything, got: {fake_run.calls}")


# ---------------------------------------------------------------------------
# cmd_steps
# ---------------------------------------------------------------------------


class CmdStepsTests(unittest.TestCase):
    """`actbreak steps` is the only way to discover a valid selector without
    reading the workflow by hand, so its output has to be paste-ready."""

    def _run_steps(self, fixture, job=None):
        args = SimpleNamespace(workflow=fixture_path(fixture), job=job)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = session.cmd_steps(args)
        return rc, buf.getvalue()

    def test_lists_every_job_and_step_with_a_pastable_selector(self):
        rc, out = self._run_steps("multi_job.yml")
        self.assertEqual(rc, 0)
        for selector in ("lint:0", "lint:1", "build:0", "build:2", "test:2"):
            self.assertIn(selector, out)
        self.assertIn("Upload artifact", out)
        # Jobs come out sorted, so the output is stable across runs.
        self.assertLess(out.index("build:"), out.index("lint:"))

    def test_job_filter_shows_only_that_job(self):
        rc, out = self._run_steps("multi_job.yml", job="lint")
        self.assertEqual(rc, 0)
        self.assertIn("lint:1", out)
        self.assertNotIn("build:", out)
        self.assertNotIn("test:", out)

    def test_unknown_job_names_the_jobs_that_do_exist(self):
        with self.assertRaises(SelectorError) as ctx:
            self._run_steps("multi_job.yml", job="nope")
        message = str(ctx.exception)
        self.assertIn("nope", message)
        self.assertIn("build", message)

    def test_unnamed_steps_are_listed_by_position(self):
        rc, out = self._run_steps("unnamed_steps.yml")
        self.assertEqual(rc, 0)
        self.assertIn("build:0", out)
        self.assertIn("(unnamed)", out)
        self.assertIn("Run tests", out)

    def test_selector_error_points_at_the_steps_command(self):
        # The dead end this command exists to fix: a bad --break-before should
        # say where to find the real names.
        text, _ = injector.read_workflow_text(fixture_path("multi_job.yml"))
        jobs = injector.parse_workflow(text.splitlines(keepends=True))
        with self.assertRaises(SelectorError) as ctx:
            resolve_selector(jobs, "Nope")
        self.assertIn("actbreak steps", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
