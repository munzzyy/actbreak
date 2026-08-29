"""Orchestration for `actbreak run`, `actbreak resume`, and `actbreak clean`.

This is the layer that actually shells out to `act` and to docker/podman.
tests/test_session.py unit-tests it against fakes (an injectable
CommandRunner, a fake Popen) the same way runtime.py is tested; the one
thing fakes can't stand in for is a real breakpoint pausing a real
container, which is covered end-to-end by the CI integration test
(tests/test_integration.py), skipped locally when docker+act aren't present.
"""

from __future__ import annotations

import json
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import injector
from .errors import (
    ActbreakError,
    AmbiguousContainerError,
    ContainerNotFoundError,
    SelectorError,
    SessionError,
)
from .runtime import CommandRunner, Container, detect_runtime, find_job_container, require_act
from .selector import resolve_selector

POLL_INTERVAL = 1.0
DEFAULT_TIMEOUT = 1800.0  # 30 minutes -- generous, but bounded

STATE_DIR = Path.home() / ".actbreak"
STATE_FILE = STATE_DIR / "state.json"


class _Interrupted(Exception):
    pass


# ---------------------------------------------------------------------------
# workflow discovery
# ---------------------------------------------------------------------------


def find_repo_root(start: Path) -> Path | None:
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".github" / "workflows").is_dir():
            return candidate
    return None


def locate_workflow(workflow_arg: str) -> tuple[Path, Path]:
    """Resolve a workflow argument (a path, or a bare name looked up under
    .github/workflows) to (workflow file path, repo root)."""
    given = Path(workflow_arg)
    if given.is_file():
        resolved = given.resolve()
        root = None
        for candidate in (resolved.parent, *resolved.parents):
            if candidate.name == ".github" and candidate.is_dir():
                root = candidate.parent
                break
        if root is None:
            root = find_repo_root(Path.cwd()) or resolved.parent
        return resolved, root

    root = find_repo_root(Path.cwd())
    if root is None:
        raise SessionError(
            f"could not find workflow '{workflow_arg}': no .github/workflows directory "
            f"found from {Path.cwd()} upward, and no such file exists"
        )
    workflows_dir = root / ".github" / "workflows"
    for candidate_name in (workflow_arg, f"{workflow_arg}.yml", f"{workflow_arg}.yaml"):
        candidate = workflows_dir / candidate_name
        if candidate.is_file():
            return candidate.resolve(), root
    raise SessionError(f"workflow '{workflow_arg}' not found in {workflows_dir}")


# ---------------------------------------------------------------------------
# session state (so `resume`/`clean` -- separate invocations -- can find
# what a still-running `run --no-attach` left behind)
# ---------------------------------------------------------------------------


def _load_sessions() -> list[dict]:
    if not STATE_FILE.is_file():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    sessions = data.get("sessions", [])
    return sessions if isinstance(sessions, list) else []


def _save_sessions(sessions: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"sessions": sessions}, indent=2), encoding="utf-8")


def _record_session(
    container: Container,
    engine: str,
    tmpdir: str | None,
    workflow: Path,
    job: str,
    label: str,
    position: str,
    pending: list[tuple[str, str]] | None = None,
) -> None:
    sessions = _load_sessions()
    sessions.append(
        {
            "container_id": container.id,
            "container_name": container.name,
            "runtime": engine,
            "tmpdir": tmpdir,
            "workflow": str(workflow),
            "job": job,
            "label": label,
            "position": position,
            # Breakpoints from the same multi-breakpoint run that are still
            # ahead of this one, as [label, position] pairs -- lets `resume`
            # step to the next one instead of running straight to completion.
            "pending": [list(p) for p in pending] if pending else [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _save_sessions(sessions)


def _cleanup_tmpdir(tmpdir: str | None) -> None:
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _build_act_command(act_bin: str, workflow_arg: str, job_name: str | None, act_args: list[str]) -> list[str]:
    cmd = [act_bin, "-W", workflow_arg, "--reuse"]
    if job_name:
        cmd += ["-j", job_name]
    cmd += act_args
    return cmd


def _attach_command_str(engine: str, container_name: str, shell: str = "sh") -> str:
    # shell may itself be more than one word ('bash -l'), so split it before
    # quoting -- otherwise the printed command comes out double-quoted and
    # pasting it runs a lookup for a binary literally named 'bash -l'.
    return " ".join(shlex.quote(p) for p in (engine, "exec", "-it", container_name, *shlex.split(shell)))


def _match_run_containers(
    containers: list[Container], job_name: str | None, jobs, workflow_hint: str | None
) -> list[Container]:
    """The containers that belong to THIS run, matched unambiguously by job
    name (narrowed by the workflow): the one job when -j was given, otherwise
    one per parsed job. Never falls back to "every act-* container", so it
    can't touch an unrelated workflow's parked debug container, and it skips
    any job whose container is absent or ambiguous -- cleanup never guesses."""
    names = [job_name] if job_name else list(jobs or [])
    matched: list[Container] = []
    for name in names:
        try:
            container = find_job_container(containers, name, workflow_hint)
        except ContainerNotFoundError:
            # Not found for this job, or ambiguous (AmbiguousContainerError is
            # a subclass) -- either way, don't guess.
            continue
        if container not in matched:
            matched.append(container)
    return matched


def _terminate_act_and_container(
    proc: subprocess.Popen, runner: CommandRunner, engine: str, job_name: str | None, jobs, workflow_hint: str | None
) -> None:
    """Best-effort cleanup for every cmd_run exit path that isn't leaving a
    supported, resumable session behind: kill `act` if it's still running
    (it was spawned start_new_session=True, so nothing else will ever reap
    it) and remove its job container(s), if any were created. Shared by
    the interrupted and the give-up-waiting (SessionError) paths."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        containers = runner.ps(engine, all_containers=True)
        for container in _match_run_containers(containers, job_name, jobs, workflow_hint):
            runner.rm_container(engine, container.name)
    except (ContainerNotFoundError, ActbreakError):
        pass


def _reap_finished_container(
    runner: CommandRunner, engine: str, job_name: str | None, jobs, workflow_hint: str | None
) -> None:
    """Remove the job container(s) `act --reuse` leaves behind after a clean run.

    actbreak passes --reuse so the container survives while the job is paused
    at the injected hold, which is the whole point -- you attach to it. But
    once the job has run to completion there is nothing left to attach to, and
    without this a normal run would leak a stopped act container every time.
    Only the intentionally-held paths keep their container: --no-attach returns
    before reaching here, and a failed --break-on-failure run hands off to the
    post-mortem, which owns that container's lifecycle instead.

    Best-effort. It only removes containers it can identify unambiguously (per
    job, by name), so it never guesses and reaps the wrong one, and a container
    that's already gone or an engine hiccup won't fail an otherwise-clean run.
    A passing --break-on-failure run has no -j, so it reaps one container per
    parsed job rather than stopping at a single global match."""
    try:
        containers = runner.ps(engine, all_containers=True)
        for container in _match_run_containers(containers, job_name, jobs, workflow_hint):
            runner.rm_container(engine, container.name)
    except (ContainerNotFoundError, ActbreakError):
        pass


def wait_for_breakpoint(
    proc: subprocess.Popen,
    runner: CommandRunner,
    engine: str,
    job_name: str,
    workflow_hint: str | None,
    interrupt_check,
    timeout: float = DEFAULT_TIMEOUT,
    shell: str = "sh",
) -> Container | None:
    """Poll until the job's container exists and has hit the hold, act exits
    first, or `timeout` elapses. Returns None if act exited before hitting it."""
    deadline = time.monotonic() + timeout
    while True:
        interrupt_check()
        if proc.poll() is not None:
            return None
        if time.monotonic() > deadline:
            raise SessionError(
                f"timed out after {int(timeout)}s waiting for job '{job_name}' to hit the breakpoint"
            )
        try:
            containers = runner.ps(engine)
            container = find_job_container(containers, job_name, workflow_hint)
        except AmbiguousContainerError as e:
            # More than one candidate container is never going to resolve
            # itself by waiting -- surface it now instead of spinning for
            # up to `timeout` and then reporting a misleading "timed out".
            # runtime.py names the candidates but doesn't know the engine, so
            # spell the attach commands out here where we do.
            message = str(e)
            if e.candidates:
                commands = " or ".join(_attach_command_str(engine, n, shell) for n in e.candidates)
                message = f"{message} Run: {commands}"
            raise SessionError(message) from e
        except ContainerNotFoundError:
            time.sleep(POLL_INTERVAL)
            continue
        if runner.file_exists(engine, container.id, "/tmp/actbreak/hold"):
            return container
        time.sleep(POLL_INTERVAL)


def _post_mortem(
    runner: CommandRunner,
    engine: str,
    job_name: str | None,
    jobs,
    workflow_hint: str | None,
    no_attach: bool,
    exit_code: int,
    shells: tuple[str, ...] = ("sh", "bash"),
) -> int:
    print(f"actbreak: act exited {exit_code}; looking for the job container for post-mortem", file=sys.stderr)
    containers = runner.ps(engine, all_containers=True)
    candidates = _match_run_containers(containers, job_name, jobs, workflow_hint)
    if not candidates:
        # No container for this run's own job(s). Never fall back to some
        # other act-* container -- attaching to (and later force-removing)
        # an unrelated workflow's parked debug session would destroy it and
        # give a confidently wrong post-mortem.
        print("actbreak: no act container found for this run's workflow for post-mortem", file=sys.stderr)
        return exit_code

    if len(candidates) > 1:
        print("actbreak: multiple job containers are still alive; attach manually:", file=sys.stderr)
        for c in candidates:
            print(f"  {_attach_command_str(engine, c.name, shells[0])}", file=sys.stderr)
        return exit_code

    container = candidates[0]
    print(f"actbreak: post-mortem container: {container.name}")
    print(f"actbreak: attach with: {_attach_command_str(engine, container.name, shells[0])}")
    if not no_attach:
        runner.exec_interactive(engine, container.name, shells=shells)
        runner.rm_container(engine, container.name)
    return exit_code


def cmd_run(args) -> int:
    workflow_path, repo_root = locate_workflow(args.workflow)
    text, _ = injector.read_workflow_text(str(workflow_path))
    lines = text.splitlines(keepends=True)
    jobs = injector.parse_workflow(lines)
    workflow_hint = injector.extract_workflow_name(lines) or workflow_path.stem

    breakpoints = list(getattr(args, "breakpoints", None) or [])
    breakpoint_requested = bool(breakpoints)
    job_name = args.job
    tmpdir = None
    act_workflow_arg = str(workflow_path)
    shell = getattr(args, "shell", None)
    shells = (shell,) if shell else ("sh", "bash")

    # hold_sequence lists every breakpoint's (label, position) in the order
    # the job will actually reach them -- see injector.inject_multi(). A
    # single --break-before/--break-after still goes through this same path
    # with a one-item sequence.
    hold_sequence: list[tuple[str, str]] = []
    if breakpoint_requested:
        targets = []
        for position, selector in breakpoints:
            hint = job_name if job_name is not None else args.job
            resolved_job, step_index = resolve_selector(jobs, selector, hint)
            if job_name is None:
                job_name = resolved_job
            targets.append((resolved_job, step_index, position))
        tmpdir = tempfile.mkdtemp(prefix="actbreak-")
        dest = str(Path(tmpdir) / workflow_path.name)
        hold_sequence = injector.inject_multi_file(str(workflow_path), dest, targets)
        act_workflow_arg = dest
        if args.verbose:
            for label, position in hold_sequence:
                print(f"actbreak: injected breakpoint {position} '{label}' -> {dest}", file=sys.stderr)

    act_bin = require_act()
    engine = detect_runtime(args.runtime)

    act_cmd = _build_act_command(act_bin, act_workflow_arg, job_name, list(args.act_arg or []))
    if args.verbose:
        print("actbreak: " + " ".join(shlex.quote(p) for p in act_cmd), file=sys.stderr)

    runner = CommandRunner()
    proc = subprocess.Popen(act_cmd, cwd=str(repo_root), start_new_session=True)

    interrupted = {"flag": False}

    def handler(signum, frame):
        interrupted["flag"] = True
        raise _Interrupted()

    old_int = signal.signal(signal.SIGINT, handler)
    old_term = signal.signal(signal.SIGTERM, handler)

    def interrupt_check():
        if interrupted["flag"]:
            raise _Interrupted()

    keep_tmpdir = False
    try:
        if breakpoint_requested:
            # remaining tracks which of hold_sequence is still ahead: popped
            # one at a time as each hold is actually reached, so a run with
            # several breakpoints steps from one to the next in a single
            # invocation instead of requiring a fresh `actbreak run` per stop.
            remaining = list(hold_sequence)
            exit_code = None
            while True:
                container = wait_for_breakpoint(
                    proc, runner, engine, job_name, workflow_hint, interrupt_check, shell=shells[0]
                )
                if container is None:
                    exit_code = proc.wait()
                    break

                label, position = remaining.pop(0)
                total = len(hold_sequence)
                hit_number = total - len(remaining)
                step_word = f" ({hit_number}/{total})" if total > 1 else ""
                print(f"actbreak: breakpoint hit{step_word} -- job '{job_name}', step '{label}' ({position})")
                print(f"actbreak: container: {container.name}")
                print(f"actbreak: attach with: {_attach_command_str(engine, container.name, shells[0])}")
                if args.no_attach:
                    _record_session(container, engine, tmpdir, workflow_path, job_name, label, position, remaining)
                    print(
                        "actbreak: --no-attach given; the container stays paused. "
                        "Run 'actbreak resume' to continue, or 'actbreak clean' to abort."
                    )
                    keep_tmpdir = True
                    return 0
                runner.exec_interactive(engine, container.name, shells=shells)
                runner.rm_file(engine, container.id, "/tmp/actbreak/hold")
                if remaining:
                    print("actbreak: resumed, waiting for the next breakpoint")
                    continue
                print("actbreak: resumed")
                exit_code = proc.wait()
                break
        else:
            exit_code = proc.wait()

        if args.break_on_failure and exit_code != 0:
            exit_code = _post_mortem(runner, engine, job_name, jobs, workflow_hint, args.no_attach, exit_code, shells)
        else:
            # The job ran to completion (resumed through the hold, never hit
            # it, or a --break-on-failure run that passed). --reuse left its
            # container behind; reap it so a clean run doesn't leak one.
            _reap_finished_container(runner, engine, job_name, jobs, workflow_hint)

        return exit_code
    except _Interrupted:
        print("\nactbreak: interrupted, cleaning up", file=sys.stderr)
        _terminate_act_and_container(proc, runner, engine, job_name, jobs, workflow_hint)
        return 130
    except SessionError:
        # wait_for_breakpoint gave up (timed out, or an ambiguous container
        # set that was never going to resolve on its own). Without this,
        # `act` -- spawned start_new_session=True -- is orphaned and keeps
        # running detached, and its job container pauses forever unattended
        # at the injected hold once it gets there, since no session was
        # ever recorded for `actbreak resume` to find. Clean up, then let
        # the SessionError keep propagating so the user still sees why.
        print("actbreak: giving up, cleaning up", file=sys.stderr)
        _terminate_act_and_container(proc, runner, engine, job_name, jobs, workflow_hint)
        raise
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        if not keep_tmpdir:
            _cleanup_tmpdir(tmpdir)


# ---------------------------------------------------------------------------
# resume / clean
# ---------------------------------------------------------------------------


def _wait_and_reap(
    runner: CommandRunner,
    engine: str,
    container_id: str,
    timeout: float = DEFAULT_TIMEOUT,
    pending: list | None = None,
) -> bool | str:
    """After `resume` drops the hold, the job runs on. Poll until it's no
    longer running, then remove it by id -- rm by id works on a stopped
    container, unlike the exec probe `clean`'s sweep uses. Returns True once
    it's gone, False if it's still running when `timeout` elapses (the caller
    then keeps the session so `clean` can reap it by id later).

    If `pending` is a non-empty list of the breakpoints still ahead (from a
    multi-breakpoint run), also watches for the hold file reappearing --
    meaning the job reached the next one instead of running to completion --
    and returns the string 'hit' in that case, without reaping anything."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            containers = runner.ps(engine, all_containers=True)
        except ActbreakError:
            return False
        match = next((c for c in containers if c.id == container_id), None)
        if match is None:
            return True  # act already removed it
        if not match.status.lower().startswith("up"):
            return runner.rm_container(engine, container_id)
        if pending and runner.file_exists(engine, container_id, "/tmp/actbreak/hold"):
            return "hit"
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def cmd_resume(args) -> int:
    sessions = _load_sessions()
    if not sessions:
        print("actbreak: no held sessions to resume", file=sys.stderr)
        return 1
    runner = CommandRunner()
    ok = True
    unresolved = []
    for i, s in enumerate(sessions):
        try:
            removed = runner.rm_file(s["runtime"], s["container_id"], "/tmp/actbreak/hold")
        except Exception as e:  # defensive: a bad/stale session entry shouldn't block the rest
            ok = False
            print(f"actbreak: failed to resume {s.get('container_name', '?')}: {e}", file=sys.stderr)
            # Its container is presumably still held -- keep the record so
            # it stays resumable/cleanable instead of only recoverable
            # through `clean`'s stray-container sweep.
            unresolved.append(s)
            # Written after every session, not once at the end: if we get
            # interrupted (Ctrl-C, or a SIGTERM that skips straight past the
            # except/finally below) while blocked on a later session, every
            # session already resolved above stays resolved on disk instead
            # of a stale, already-gone entry lingering in STATE_FILE.
            _save_sessions(unresolved + sessions[i + 1 :])
            continue
        if not removed:
            # The hold couldn't be removed: the container isn't running (e.g.
            # it stopped across a reboot), so there's nothing to resume into.
            # Keep the record -- `clean` reaps it by id -- instead of falsely
            # reporting success and dropping it into an unrecoverable orphan.
            ok = False
            print(
                f"actbreak: could not resume {s.get('container_name', '?')}: its container "
                "isn't running. Run 'actbreak clean' to remove it.",
                file=sys.stderr,
            )
            unresolved.append(s)
            _save_sessions(unresolved + sessions[i + 1 :])
            continue
        print(f"actbreak: resumed {s['container_name']}")
        # A session recorded from a multi-breakpoint run carries the
        # breakpoints still ahead of the one just resumed; a job that reaches
        # one of those before finishing hands back "hit" instead of running
        # to completion, and we re-park at that breakpoint instead of
        # treating the job as done.
        pending = list(s.get("pending") or [])
        # The job now runs to completion and `act --reuse` leaves the container
        # stopped; wait for it and reap it so resume doesn't leak one. If it
        # outlives the wait, keep the record so `clean` can still get it.
        # Say that we're waiting: the rest of the workflow can take a while
        # and without this the command just sits there looking hung.
        print(
            f"actbreak: waiting for {s['container_name']} to finish "
            f"(Ctrl-C to leave it running; 'actbreak clean' reaps it later)"
        )
        try:
            result = _wait_and_reap(runner, s["runtime"], s["container_id"], pending=pending)
        except KeyboardInterrupt:
            # Ctrl-C means "stop watching", not "abort the job". Keep this
            # session and every one still queued behind it so they stay
            # resumable and cleanable.
            print(
                "\nactbreak: stopped waiting; the job is still running. "
                "Run 'actbreak clean' once it's done.",
                file=sys.stderr,
            )
            unresolved.extend(sessions[i:])
            _save_sessions(unresolved)
            return 0
        if result == "hit":
            next_label, next_position = pending[0]
            print(f"actbreak: breakpoint hit -- step '{next_label}' ({next_position})")
            print(f"actbreak: container: {s['container_name']}")
            print(f"actbreak: attach with: {_attach_command_str(s['runtime'], s['container_name'])}")
            print("actbreak: run 'actbreak resume' again to continue, or 'actbreak clean' to abort.")
            s = dict(s, label=next_label, position=next_position, pending=pending[1:])
            unresolved.append(s)
        elif result:
            _cleanup_tmpdir(s.get("tmpdir"))
        else:
            unresolved.append(s)
        _save_sessions(unresolved + sessions[i + 1 :])
    return 0 if ok else 1


def cmd_clean(args) -> int:
    sessions = _load_sessions()
    runner = CommandRunner()
    for s in sessions:
        try:
            if runner.rm_container(s["runtime"], s["container_id"]):
                print(f"actbreak: cleaned {s.get('container_name', s['container_id'])}")
            else:
                print(f"actbreak: failed to clean {s.get('container_name', '?')}", file=sys.stderr)
        except Exception as e:  # defensive
            print(f"actbreak: failed to clean {s.get('container_name', '?')}: {e}", file=sys.stderr)
        _cleanup_tmpdir(s.get("tmpdir"))
    _save_sessions([])

    # Best-effort sweep for stray act-* containers we lost track of (e.g. the
    # state file was deleted, or actbreak crashed before recording a session).
    for engine in ("docker", "podman"):
        if shutil.which(engine) is None:
            continue
        try:
            containers = runner.ps(engine, all_containers=True)
        except Exception:
            continue
        for c in containers:
            if not c.name.lower().startswith("act-"):
                continue
            if runner.file_exists(engine, c.id, "/tmp/actbreak/hold"):
                runner.rm_container(engine, c.id)
                print(f"actbreak: cleaned stray container {c.name}")
    return 0


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------


def cmd_steps(args) -> int:
    """Print the steps a workflow offers, selector first, so there's a way to
    find a valid selector short of reading the YAML and counting by hand."""
    workflow_path, _ = locate_workflow(args.workflow)
    text, _ = injector.read_workflow_text(str(workflow_path))
    jobs = injector.parse_workflow(text.splitlines(keepends=True))

    job_filter = getattr(args, "job", None)
    if job_filter is not None and job_filter not in jobs:
        raise SelectorError(
            f"job '{job_filter}' not found (available jobs: {', '.join(sorted(jobs)) or 'none'})"
        )
    wanted = [job_filter] if job_filter else sorted(jobs)

    total = 0
    for job_name in wanted:
        steps = jobs[job_name].steps
        print(f"{job_name}:")
        if not steps:
            print("  (no steps)")
            continue
        width = max(len(f"{job_name}:{s.index}") for s in steps)
        for step in steps:
            selector = f"{job_name}:{step.index}"
            print(f"  {selector.ljust(width)}  {step.name if step.name else '(unnamed)'}")
        total += len(steps)

    if total == 0:
        print(f"actbreak: no steps to break on in {workflow_path}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _container_status(runner: CommandRunner, engine: str, container_id: str, cache: dict) -> str:
    """Report a recorded session's container as 'running', 'stopped', or 'gone'
    using the same `ps -a` listing the rest of the tool tracks containers by
    (id match, then the 'Up ...' status prefix, exactly as _wait_and_reap
    reads it). `cache` memoizes the per-engine listing so a batch of sessions
    on one runtime only lists once. A missing/broken runtime -- e.g. the state
    file names podman but it's since been uninstalled -- yields 'unknown'
    rather than crashing the whole listing."""
    if engine not in cache:
        try:
            cache[engine] = runner.ps(engine, all_containers=True)
        except Exception:  # defensive: a missing/broken runtime shouldn't sink the list
            cache[engine] = None
    containers = cache[engine]
    if containers is None:
        return "unknown"
    match = next((c for c in containers if c.id == container_id), None)
    if match is None:
        return "gone"
    return "running" if match.status.lower().startswith("up") else "stopped"


def _session_age(created_at: str | None) -> str | None:
    """Format how long a session has been parked, as 'Xm' under an hour or
    'Xh' from there -- so `actbreak list` can flag which held session has
    been sitting for 5 minutes vs 5 hours (a likely orphan worth `clean`ing).
    Returns None for a missing or unparseable timestamp (an older state file
    predating this field, say) rather than raising."""
    if not created_at:
        return None
    try:
        started = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    minutes = max(int((datetime.now(timezone.utc) - started).total_seconds() // 60), 0)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h"


def cmd_list(args) -> int:
    """Show the debug sessions parked by `run --no-attach` (or a resume/clean
    that couldn't finish), each annotated with its live container status so you
    can see which breakpoints are still held and which are orphans to reap."""
    sessions = _load_sessions()
    if not sessions:
        print("actbreak: no parked debug sessions")
        return 0

    runner = CommandRunner()
    status_cache: dict = {}
    noun = "session" if len(sessions) == 1 else "sessions"
    print(f"actbreak: {len(sessions)} parked debug {noun}:")
    for s in sessions:
        engine = s.get("runtime", "")
        status = _container_status(runner, engine, s.get("container_id", ""), status_cache)
        name = s.get("container_name") or s.get("container_id") or "?"
        job = s.get("job") or "?"
        label = s.get("label") or "?"
        position = s.get("position") or "?"
        workflow = s.get("workflow") or "?"
        age = _session_age(s.get("created_at"))
        age_suffix = f", held for {age}" if age else ""
        print(f"  {name} [{status}] -- job '{job}', step '{label}' ({position}){age_suffix} -- {workflow}")
    return 0
