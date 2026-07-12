"""Container runtime detection (docker/podman) and `act` container discovery.

act names the containers it starts after the workflow and job, e.g.
`act-Build-and-Test-build-...`. We list running containers with a fixed,
tab-separated Go template so docker and podman (podman's CLI is
docker-compatible) produce identical output, then match on a normalized
substring of the job (and optionally workflow) name.

All subprocess calls go through an injectable runner so tests never touch a
real docker/podman binary.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

from .errors import ContainerNotFoundError, ToolNotFoundError

PS_FORMAT = "{{.ID}}\t{{.Names}}\t{{.Status}}"

ACT_INSTALL_HINT = (
    "act was not found on PATH.\n"
    "Install it with one of:\n"
    "  brew install act                       (macOS/Linux, Homebrew)\n"
    "  gh extension install https://github.com/nektos/gh-act\n"
    "  choco install act-cli                   (Windows)\n"
    "  curl -s https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash\n"
    "See https://github.com/nektos/act#installation for details."
)


@dataclass(frozen=True)
class Container:
    id: str
    name: str
    status: str = ""


def parse_ps_output(output: str) -> list[Container]:
    """Parse output produced by `ps --format {PS_FORMAT}`."""
    containers = []
    for raw_line in output.splitlines():
        line = raw_line.strip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        cid = parts[0].strip()
        name = parts[1].strip()
        status = parts[2].strip() if len(parts) > 2 else ""
        if not cid or not name:
            continue
        containers.append(Container(id=cid, name=name, status=status))
    return containers


def normalize_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _name_tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]


def _job_in_name(name: str, job: str) -> bool:
    """True when `job` appears as a run of whole delimited tokens in `name`,
    not just as a substring. Keeps job 'a' or 'test' from matching inside an
    unrelated token like 'latest', while still matching a multi-word job
    ('say hello' -> the tokens say, hello) across act's dash/underscore joins."""
    ntoks = _name_tokens(name)
    jtoks = _name_tokens(job)
    if not jtoks:
        return False
    return any(ntoks[i:i + len(jtoks)] == jtoks for i in range(len(ntoks) - len(jtoks) + 1))


def find_job_container(
    containers: list[Container], job: str, workflow: str | None = None
) -> Container:
    """Find the single container act started for `job` (optionally narrowed
    by `workflow`). Raises ContainerNotFoundError if there's none or more
    than one match."""
    candidates = [c for c in containers if c.name.lower().startswith("act-") and _job_in_name(c.name, job)]

    if workflow:
        nwf = normalize_name(workflow)
        narrowed = [c for c in candidates if nwf in normalize_name(c.name)]
        if narrowed:
            candidates = narrowed

    if not candidates:
        seen = ", ".join(c.name for c in containers if c.name.lower().startswith("act-")) or "none"
        raise ContainerNotFoundError(
            f"no running act container found for job '{job}' (act containers seen: {seen})"
        )
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        raise ContainerNotFoundError(
            f"multiple containers match job '{job}': {names}; narrow with the workflow name"
        )
    return candidates[0]


def detect_runtime(preference: str = "auto", which=shutil.which) -> str:
    """Return 'docker' or 'podman'. `preference` is 'auto', 'docker', or 'podman'."""
    if preference in ("docker", "podman"):
        if which(preference) is None:
            raise ToolNotFoundError(
                f"--runtime {preference} was requested but '{preference}' was not found on PATH"
            )
        return preference
    if preference != "auto":
        raise ValueError(f"invalid runtime preference: {preference!r}")
    for candidate in ("docker", "podman"):
        if which(candidate):
            return candidate
    raise ToolNotFoundError(
        "neither docker nor podman was found on PATH. actbreak needs one of them "
        "to attach to the job container it's debugging. Install Docker "
        "(https://docs.docker.com/get-docker/) or Podman (https://podman.io/getting-started/installation)."
    )


def require_act(which=shutil.which) -> str:
    """Return the resolved path to `act`, or raise ToolNotFoundError."""
    path = which("act")
    if path is None:
        raise ToolNotFoundError(ACT_INSTALL_HINT)
    return path


class CommandRunner:
    """Thin, injectable wrapper around subprocess calls to docker/podman.

    `run` defaults to subprocess.run; tests pass a fake that returns canned
    CompletedProcess-like objects and records the argv it was called with.
    """

    def __init__(self, run=subprocess.run):
        self._run = run

    def ps(self, engine: str, all_containers: bool = False) -> list[Container]:
        args = [engine, "ps", "--format", PS_FORMAT]
        if all_containers:
            args.append("-a")
        result = self._run(args, capture_output=True, text=True, check=False)
        return parse_ps_output(result.stdout or "")

    def file_exists(self, engine: str, container: str, path: str) -> bool:
        # Capture output: these run inside the job container while we poll, and a
        # workflow that swapped test/rm for something spewing ANSI escapes could
        # otherwise write straight to the developer's terminal.
        result = self._run([engine, "exec", container, "test", "-f", path],
                           capture_output=True, text=True, check=False)
        return getattr(result, "returncode", 1) == 0

    def rm_file(self, engine: str, container: str, path: str) -> None:
        self._run([engine, "exec", container, "rm", "-f", path],
                  capture_output=True, text=True, check=False)

    def exec_interactive(self, engine: str, container: str, shells: tuple[str, ...] = ("sh", "bash")) -> int:
        """Attempt an interactive exec shell, trying each entry in `shells`
        in turn (falling back on the shell-not-found exit codes 126/127)."""
        returncode = 127
        for shell in shells:
            result = self._run([engine, "exec", "-it", container, shell], check=False)
            returncode = getattr(result, "returncode", 1)
            if returncode not in (126, 127):
                return returncode
        return returncode

    def rm_container(self, engine: str, container: str, force: bool = True) -> None:
        args = [engine, "rm"]
        if force:
            args.append("-f")
        args.append(container)
        self._run(args, capture_output=True, text=True, check=False)
