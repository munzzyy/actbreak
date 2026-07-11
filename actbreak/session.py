"""Orchestration for `actbreak run`, `actbreak resume`, and `actbreak clean`.

This is the layer that actually shells out to `act` and to docker/podman.
It can't be meaningfully unit tested without a real container runtime and a
real `act` binary -- that end-to-end path is covered by the CI integration
test (tests/test_integration.py), which is skipped locally when docker+act
aren't present. The pieces it's built from (injector, selector, runtime
parsing) are fully unit tested; this module is where they're wired together.
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
from .errors import ActbreakError, ContainerNotFoundError, SessionError
from .runtime import CommandRunner, Container, detect_runtime, find_job_container, normalize_name, require_act
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
    container: Container, engine: str, tmpdir: str | None, workflow: Path, job: str, label: str, position: str
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
    return " ".join(shlex.quote(p) for p in (engine, "exec", "-it", container_name, shell))


def wait_for_breakpoint(
    proc: subprocess.Popen,
    runner: CommandRunner,
    engine: str,
    job_name: str,
    workflow_hint: str | None,
    interrupt_check,
    timeout: float = DEFAULT_TIMEOUT,
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
        except ContainerNotFoundError:
            time.sleep(POLL_INTERVAL)
            continue
        if runner.file_exists(engine, container.id, "/tmp/actbreak/hold"):
            return container
        time.sleep(POLL_INTERVAL)


def _post_mortem(
    runner: CommandRunner, engine: str, job_name: str | None, workflow_hint: str | None, no_attach: bool, exit_code: int
) -> int:
    print(f"actbreak: act exited {exit_code}; looking for the job container for post-mortem", file=sys.stderr)
    containers = runner.ps(engine, all_containers=True)
    act_containers = [c for c in containers if c.name.lower().startswith("act-")]
    if job_name:
        try:
            container = find_job_container(containers, job_name, workflow_hint)
        except ContainerNotFoundError as e:
            print(f"actbreak: {e}", file=sys.stderr)
            return exit_code
        candidates = [container]
    else:
        if workflow_hint:
            nwf = normalize_name(workflow_hint)
            candidates = [c for c in act_containers if nwf in normalize_name(c.name)] or act_containers
        else:
            candidates = act_containers
        if not candidates:
            print("actbreak: no act container found for post-mortem", file=sys.stderr)
            return exit_code

    if len(candidates) > 1:
        print("actbreak: multiple job containers are still alive; attach manually:", file=sys.stderr)
        for c in candidates:
            print(f"  {_attach_command_str(engine, c.name)}", file=sys.stderr)
        return exit_code

    container = candidates[0]
    print(f"actbreak: post-mortem container: {container.name}")
    print(f"actbreak: attach with: {_attach_command_str(engine, container.name)}")
    if not no_attach:
        runner.exec_interactive(engine, container.name)
        runner.rm_container(engine, container.name)
    return exit_code


def cmd_run(args) -> int:
    workflow_path, repo_root = locate_workflow(args.workflow)
    text, _ = injector.read_workflow_text(str(workflow_path))
    lines = text.splitlines(keepends=True)
    jobs = injector.parse_workflow(lines)
    workflow_hint = injector.extract_workflow_name(lines) or workflow_path.stem

    breakpoint_requested = args.break_before is not None or args.break_after is not None
    job_name = args.job
    label = None
    position = None
    tmpdir = None
    act_workflow_arg = str(workflow_path)

    if breakpoint_requested:
        selector = args.break_before if args.break_before is not None else args.break_after
        position = "before" if args.break_before is not None else "after"
        job_name, step_index = resolve_selector(jobs, selector, args.job)
        tmpdir = tempfile.mkdtemp(prefix="actbreak-")
        dest = str(Path(tmpdir) / workflow_path.name)
        label = injector.inject_file(str(workflow_path), dest, job_name, step_index, position)
        act_workflow_arg = dest
        if args.verbose:
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
            container = wait_for_breakpoint(proc, runner, engine, job_name, workflow_hint, interrupt_check)
            if container is None:
                exit_code = proc.wait()
            else:
                print(f"actbreak: breakpoint hit -- job '{job_name}', step '{label}' ({position})")
                print(f"actbreak: container: {container.name}")
                print(f"actbreak: attach with: {_attach_command_str(engine, container.name)}")
                if args.no_attach:
                    _record_session(container, engine, tmpdir, workflow_path, job_name, label, position)
                    print(
                        "actbreak: --no-attach given; the container stays paused. "
                        "Run 'actbreak resume' to continue, or 'actbreak clean' to abort."
                    )
                    keep_tmpdir = True
                    return 0
                runner.exec_interactive(engine, container.name)
                runner.rm_file(engine, container.id, "/tmp/actbreak/hold")
                print("actbreak: resumed")
                exit_code = proc.wait()
        else:
            exit_code = proc.wait()

        if args.break_on_failure and exit_code != 0:
            exit_code = _post_mortem(runner, engine, job_name, workflow_hint, args.no_attach, exit_code)

        return exit_code
    except _Interrupted:
        print("\nactbreak: interrupted, cleaning up", file=sys.stderr)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            containers = runner.ps(engine, all_containers=True)
            container = find_job_container(containers, job_name, workflow_hint) if job_name else None
            if container is not None:
                runner.rm_container(engine, container.name)
        except (ContainerNotFoundError, ActbreakError):
            pass
        return 130
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        if not keep_tmpdir:
            _cleanup_tmpdir(tmpdir)


# ---------------------------------------------------------------------------
# resume / clean
# ---------------------------------------------------------------------------


def cmd_resume(args) -> int:
    sessions = _load_sessions()
    if not sessions:
        print("actbreak: no held sessions to resume", file=sys.stderr)
        return 1
    runner = CommandRunner()
    ok = True
    for s in sessions:
        try:
            runner.rm_file(s["runtime"], s["container_id"], "/tmp/actbreak/hold")
            print(f"actbreak: resumed {s['container_name']}")
        except Exception as e:  # defensive: a bad/stale session entry shouldn't block the rest
            ok = False
            print(f"actbreak: failed to resume {s.get('container_name', '?')}: {e}", file=sys.stderr)
        _cleanup_tmpdir(s.get("tmpdir"))
    _save_sessions([])
    return 0 if ok else 1


def cmd_clean(args) -> int:
    sessions = _load_sessions()
    runner = CommandRunner()
    for s in sessions:
        try:
            runner.rm_container(s["runtime"], s["container_id"])
            print(f"actbreak: cleaned {s.get('container_name', s['container_id'])}")
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
