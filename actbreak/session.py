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
from .selector import resolve_breakpoints

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
    position: str | None,
    pending: list[tuple[str, str]] | None = None,
    shell: str | None = None,
    post_mortem_exit: int | None = None,
) -> None:
    entry = {
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
    # Optional keys: state files written before they existed lack them.
    if shell:
        entry["shell"] = shell
    if post_mortem_exit is not None:
        entry["post_mortem"] = True
        entry["exit_code"] = post_mortem_exit
    sessions = _load_sessions()
    sessions.append(entry)
    _save_sessions(sessions)


def _cleanup_tmpdir(tmpdir: str | None) -> None:
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _build_act_command(
    act_bin: str, workflow_arg: str, job_name: str | None, act_args: list[str], matrix: list[str] = ()
) -> list[str]:
    cmd = [act_bin, "-W", workflow_arg, "--reuse"]
    if job_name:
        cmd += ["-j", job_name]
    for pair in matrix:
        cmd += ["--matrix", pair]
    cmd += act_args
    return cmd


def _attach_command_str(engine: str, container_name: str, shell: str = "sh") -> str:
    # shell may itself be more than one word ('bash -l'), so split it before
    # quoting -- otherwise the printed command comes out double-quoted and
    # pasting it runs a lookup for a binary literally named 'bash -l'.
    return " ".join(shlex.quote(p) for p in (engine, "exec", "-it", container_name, *shlex.split(shell)))


def _never_reached(job: str, missed: list) -> str:
    noun = "breakpoint" if len(missed) == 1 else f"{len(missed)} breakpoints"
    where = ", ".join(f"step '{label}' ({position})" for label, position in missed)
    return f"actbreak: {noun} never reached -- job '{job}', {where}"


def _match_run_jobs(
    containers: list[Container], job_name: str | None, jobs, workflow_hint: str | None
) -> list[tuple[str, Container]]:
    """The containers that belong to THIS run, each with the job it was
    matched to, matched unambiguously by job name (narrowed by the
    workflow): the one job when -j was given, otherwise one per parsed job.
    Never falls back to "every act-* container", so it can't touch an
    unrelated workflow's parked debug container, and it skips any job whose
    container is absent or ambiguous -- cleanup never guesses."""
    names = [job_name] if job_name else list(jobs or [])
    matched: list[tuple[str, Container]] = []
    for name in names:
        try:
            container = find_job_container(containers, name, workflow_hint)
        except ContainerNotFoundError:
            # Not found for this job, or ambiguous (AmbiguousContainerError is
            # a subclass) -- either way, don't guess.
            continue
        if all(container != c for _, c in matched):
            matched.append((name, container))
    return matched


def _match_run_containers(
    containers: list[Container], job_name: str | None, jobs, workflow_hint: str | None
) -> list[Container]:
    return [c for _, c in _match_run_jobs(containers, job_name, jobs, workflow_hint)]


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
    first, or `timeout` elapses (a timeout of 0 never elapses). Returns None
    if act exited before hitting it."""
    deadline = None if timeout == 0 else time.monotonic() + timeout
    while True:
        interrupt_check()
        if proc.poll() is not None:
            return None
        if deadline is not None and time.monotonic() > deadline:
            raise SessionError(
                f"timed out after {int(timeout)}s waiting for job '{job_name}' to hit the breakpoint; "
                "a long build or a first image pull can take longer, so raise --timeout "
                "(or pass --timeout 0 to wait as long as act runs)"
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


def _parked_job_containers(
    runner: CommandRunner, engine: str, job_names: list[str], workflow_hint: str | None
) -> list[Container]:
    """Running containers wait_for_breakpoint would take for one of these jobs."""
    containers = runner.ps(engine)
    found: list[Container] = []
    for job in job_names:
        try:
            candidates = [find_job_container(containers, job, workflow_hint)]
        except AmbiguousContainerError as e:
            candidates = [c for c in containers if c.name in e.candidates]
        except ContainerNotFoundError:
            candidates = []
        found += [c for c in candidates if c not in found]
    return found


def _refuse_if_job_parked(
    runner: CommandRunner, engine: str, workflow: Path, job_names: list[str], workflow_hint: str | None
) -> None:
    """Raise SessionError while one of these jobs still has a container held
    at a breakpoint. A new run would find that hold on its first poll and
    report a hit at the wrong step, and releasing either would release both."""
    sessions = _load_sessions()
    cache: dict = {}
    for s in sessions:
        if s.get("workflow") != str(workflow) or s.get("job") not in job_names:
            continue
        status = _container_status(runner, s.get("runtime", ""), s.get("container_id", ""), cache)
        if status not in ("running", "stopped"):
            continue
        name = s.get("container_name") or s.get("container_id") or "?"
        cid = s.get("container_id")
        held_in = f"{name} ({cid})" if cid and cid != name else name
        if s.get("post_mortem"):
            where, fix = "as a post-mortem container", f"'actbreak clean {name}'"
        else:
            where = f"at step '{s.get('label', '?')}' ({s.get('position', '?')})"
            fix = f"'actbreak resume {name}' or 'actbreak clean {name}'"
        raise SessionError(
            f"job '{s['job']}' is still parked {where} in {held_in}, and a new run of it would stop "
            f"at that hold instead of its own. Run {fix} first"
        )

    known = {s.get("container_id") for s in sessions}
    for c in _parked_job_containers(runner, engine, job_names, workflow_hint):
        if c.id not in known and runner.file_exists(engine, c.id, "/tmp/actbreak/hold"):
            raise SessionError(
                f"{c.name} ({c.id}) is still holding at an actbreak breakpoint that no session "
                "records, and a new run of this job would stop at that hold instead of its own. "
                f"Remove it first with '{engine} rm -f {c.name}' (a plain 'actbreak clean' removes "
                "it too, along with every parked session)"
            )


def _post_mortem(
    runner: CommandRunner,
    engine: str,
    workflow: Path,
    job_name: str | None,
    jobs,
    workflow_hint: str | None,
    no_attach: bool,
    exit_code: int,
    shells: tuple[str, ...] = ("sh", "bash"),
    shell: str | None = None,
) -> int:
    print(f"actbreak: act exited {exit_code}; looking for the job container for post-mortem", file=sys.stderr)
    containers = runner.ps(engine, all_containers=True)
    matched = _match_run_jobs(containers, job_name, jobs, workflow_hint)
    candidates = [c for _, c in matched]

    def park(job: str, container: Container) -> None:
        _record_session(
            container, engine, None, workflow, job, None, None, shell=shell, post_mortem_exit=exit_code
        )

    if not candidates:
        # No container for this run's own job(s). Never fall back to some
        # other act-* container -- attaching to (and later force-removing)
        # an unrelated workflow's parked debug session would destroy it and
        # give a confidently wrong post-mortem.
        print("actbreak: no act container found for this run's workflow for post-mortem", file=sys.stderr)
        return exit_code

    if len(candidates) > 1:
        print("actbreak: multiple job containers are still alive; attach manually:", file=sys.stderr)
        for job, c in matched:
            print(f"  {_attach_command_str(engine, c.name, shells[0])}", file=sys.stderr)
            park(job, c)
        print("actbreak: 'actbreak clean' removes them when you're done.", file=sys.stderr)
        return exit_code

    job, container = matched[0]
    print(f"actbreak: post-mortem container: {container.name}")
    print(f"actbreak: attach with: {_attach_command_str(engine, container.name, shells[0])}")
    if no_attach:
        park(job, container)
        print("actbreak: --no-attach given; the container is kept. Run 'actbreak clean' when you're done.")
    else:
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
    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT)

    # hold_sequence lists every breakpoint's (label, position) in the order
    # the job will actually reach them -- see injector.inject_multi(). A
    # single --break-before/--break-after still goes through this same path
    # with a one-item sequence.
    hold_sequence: list[tuple[str, str]] = []
    if breakpoint_requested:
        job_name, targets = resolve_breakpoints(jobs, breakpoints, args.job)
        tmpdir = tempfile.mkdtemp(prefix="actbreak-")
        dest = str(Path(tmpdir) / workflow_path.name)
        hold_sequence = injector.inject_multi_file(str(workflow_path), dest, targets)
        act_workflow_arg = dest
        if args.verbose:
            for label, position in hold_sequence:
                print(f"actbreak: injected breakpoint {position} '{label}' -> {dest}", file=sys.stderr)

    act_bin = require_act()
    engine = detect_runtime(args.runtime)
    runner = CommandRunner()
    try:
        _refuse_if_job_parked(runner, engine, workflow_path, [job_name] if job_name else list(jobs), workflow_hint)
    except SessionError:
        _cleanup_tmpdir(tmpdir)
        raise

    act_cmd = _build_act_command(
        act_bin, act_workflow_arg, job_name, list(args.act_arg or []), list(getattr(args, "matrix", None) or [])
    )
    if args.verbose:
        print("actbreak: " + " ".join(shlex.quote(p) for p in act_cmd), file=sys.stderr)

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
                    proc, runner, engine, job_name, workflow_hint, interrupt_check,
                    timeout=timeout, shell=shells[0],
                )
                if container is None:
                    exit_code = proc.wait()
                    print(f"{_never_reached(job_name, remaining)}; act exited {exit_code}", file=sys.stderr)
                    print(
                        "actbreak: a job skipped by its `if:`, or by a `needs:` job that failed, never "
                        "runs its steps; act's output above has the details",
                        file=sys.stderr,
                    )
                    break

                label, position = remaining.pop(0)
                total = len(hold_sequence)
                hit_number = total - len(remaining)
                step_word = f" ({hit_number}/{total})" if total > 1 else ""
                print(f"actbreak: breakpoint hit{step_word} -- job '{job_name}', step '{label}' ({position})")
                print(f"actbreak: container: {container.name}")
                print(f"actbreak: attach with: {_attach_command_str(engine, container.name, shells[0])}")
                if args.no_attach:
                    _record_session(
                        container, engine, tmpdir, workflow_path, job_name, label, position, remaining,
                        shell=shell,
                    )
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
            exit_code = _post_mortem(
                runner, engine, workflow_path, job_name, jobs, workflow_hint, args.no_attach, exit_code,
                shells, shell=shell,
            )
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
    it's gone, 'timeout' if it's still running when `timeout` elapses, and
    False if it couldn't be listed or removed. Either way the caller keeps
    the session so `clean` can reap it by id later.

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
            return "timeout"
        time.sleep(POLL_INTERVAL)


def _select_sessions(sessions: list[dict], query: str | None) -> list[int]:
    """Indices of the sessions `query` names: every session when it's None,
    otherwise the one whose container name or id is `query`, or failing
    that the one it's a unique prefix of."""
    if query is None:
        return list(range(len(sessions)))

    def keys(s: dict) -> list[str]:
        return [k for k in (s.get("container_name"), s.get("container_id")) if k]

    matches = [i for i, s in enumerate(sessions) if query in keys(s)]
    if not matches:
        matches = [i for i, s in enumerate(sessions) if any(k.startswith(query) for k in keys(s))]
    if len(matches) == 1:
        return matches

    def label(i: int) -> str:
        return sessions[i].get("container_name") or sessions[i].get("container_id") or "?"

    if not matches:
        parked = ", ".join(label(i) for i in range(len(sessions))) or "none"
        raise SessionError(f"no parked session matches '{query}' (parked: {parked})")
    raise SessionError(
        f"'{query}' matches {len(matches)} parked sessions: {', '.join(label(i) for i in matches)}; "
        "give more of the container name"
    )


def cmd_resume(args) -> int:
    sessions = _load_sessions()
    if not sessions:
        print("actbreak: no held sessions to resume", file=sys.stderr)
        return 1
    picked = _select_sessions(sessions, getattr(args, "session", None))
    # None marks a finished session; saved after each one so an interrupt can't leave it in STATE_FILE.
    slots: list[dict | None] = list(sessions)

    def save() -> None:
        _save_sessions([s for s in slots if s is not None])

    todo = []
    for i in picked:
        s = sessions[i]
        if s.get("post_mortem"):
            # No hold to drop in a post-mortem container: act already exited.
            print(
                f"actbreak: nothing to resume in {s.get('container_name', '?')}: it's a post-mortem "
                "container and act has already exited. Run 'actbreak clean' to remove it.",
                file=sys.stderr,
            )
        else:
            todo.append(i)
    if not todo:
        return 1

    runner = CommandRunner()
    ok = True
    for i in todo:
        s = sessions[i]
        try:
            removed = runner.rm_file(s["runtime"], s["container_id"], "/tmp/actbreak/hold")
        except Exception as e:  # defensive: a bad/stale session entry shouldn't block the rest
            ok = False
            print(f"actbreak: failed to resume {s.get('container_name', '?')}: {e}", file=sys.stderr)
            continue
        if not removed:
            # Not running (stopped across a reboot, say): nothing to resume into, keep it for `clean`.
            ok = False
            print(
                f"actbreak: could not resume {s.get('container_name', '?')}: its container "
                "isn't running. Run 'actbreak clean' to remove it.",
                file=sys.stderr,
            )
            continue
        print(f"actbreak: resumed {s['container_name']}")
        # Breakpoints still ahead: if the job reaches one, re-park there instead of reaping.
        pending = list(s.get("pending") or [])
        print(
            f"actbreak: waiting for {s['container_name']} to finish "
            f"(Ctrl-C to leave it running; 'actbreak clean' reaps it later)"
        )
        try:
            result = _wait_and_reap(runner, s["runtime"], s["container_id"], pending=pending)
        except KeyboardInterrupt:
            # "Stop watching", not "abort the job": this session and the ones behind it stay parked.
            print(
                "\nactbreak: stopped waiting; the job is still running. "
                "Run 'actbreak clean' once it's done.",
                file=sys.stderr,
            )
            save()
            return 0
        if result == "hit":
            next_label, next_position = pending[0]
            print(f"actbreak: breakpoint hit -- step '{next_label}' ({next_position})")
            print(f"actbreak: container: {s['container_name']}")
            attach = _attach_command_str(s["runtime"], s["container_name"], s.get("shell") or "sh")
            print(f"actbreak: attach with: {attach}")
            print("actbreak: run 'actbreak resume' again to continue, or 'actbreak clean' to abort.")
            slots[i] = dict(s, label=next_label, position=next_position, pending=pending[1:])
        elif result == "timeout":
            print(
                f"actbreak: gave up waiting for {s['container_name']} after {int(DEFAULT_TIMEOUT // 60)} minutes; "
                "the job is still running. Run 'actbreak clean' once it's done.",
                file=sys.stderr,
            )
        elif result:
            if pending:
                print(f"{_never_reached(s.get('job', '?'), pending)}; the job finished first", file=sys.stderr)
            _cleanup_tmpdir(s.get("tmpdir"))
            slots[i] = None
        else:
            print(
                f"actbreak: couldn't reap {s['container_name']}; run 'actbreak clean' to remove it.",
                file=sys.stderr,
            )
        save()
    return 0 if ok else 1


def _container_gone(runner: CommandRunner, engine: str, container_id: str) -> bool:
    """True only when the engine answers and doesn't list the container. A
    listing that fails (daemon down, runtime uninstalled) proves nothing."""
    try:
        containers = runner.ps(engine, all_containers=True, strict=True)
    except Exception:
        return False
    return all(c.id != container_id for c in containers)


def cmd_clean(args) -> int:
    sessions = _load_sessions()
    query = getattr(args, "session", None)
    picked = set(_select_sessions(sessions, query))
    runner = CommandRunner()
    ok = True
    kept = []
    for i, s in enumerate(sessions):
        if i not in picked:
            kept.append(s)
            continue
        name = s.get("container_name", s.get("container_id", "?"))
        try:
            removed = runner.rm_container(s["runtime"], s["container_id"])
            error = ""
        except Exception as e:  # defensive
            removed = False
            error = f": {e}"
        if removed:
            print(f"actbreak: cleaned {name}")
        elif not error and _container_gone(runner, s["runtime"], s["container_id"]):
            print(f"actbreak: {name} was already gone")
        else:
            ok = False
            print(f"actbreak: failed to clean {name}{error}; keeping it for the next 'actbreak clean'", file=sys.stderr)
            kept.append(s)
            continue
        _cleanup_tmpdir(s.get("tmpdir"))
    _save_sessions(kept)
    if query is not None:
        # The sweep would also take every other parked session's container.
        return 0 if ok else 1

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
                if runner.rm_container(engine, c.id):
                    print(f"actbreak: cleaned stray container {c.name}")
                else:
                    ok = False
                    print(f"actbreak: failed to clean stray container {c.name}", file=sys.stderr)
    return 0 if ok else 1


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
        if s.get("post_mortem"):
            where = f"post-mortem after act exited {s.get('exit_code', '?')}"
        else:
            where = f"step '{label}' ({position})"
        print(f"  {name} [{status}] -- job '{job}', {where}{age_suffix} -- {workflow}")
    return 0
