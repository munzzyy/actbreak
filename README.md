# actbreak

[![CI](https://github.com/munzzyy/actbreak/actions/workflows/ci.yml/badge.svg)](https://github.com/munzzyy/actbreak/actions/workflows/ci.yml)
[![License: Prosperity 3.0.0](https://img.shields.io/badge/license-Prosperity--3.0.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

A local breakpoint debugger for [`act`](https://github.com/nektos/act). `act` runs
GitHub Actions workflows locally, but there's no way to pause mid-run and look
around. actbreak injects a real breakpoint into a workflow, waits for the job
container to reach it, and drops you into a live shell inside the still-running
container. Resume when you're done, and the workflow keeps going.

Zero runtime dependencies. Python 3.9+, stdlib only.

## Status

Early / v0.1.0. Core injection and selection logic is unit tested; the
run/resume/clean orchestration against real `act` + Docker/Podman is covered by
a CI integration test rather than exercised in every environment this ships to.

## Install

```
git clone https://github.com/munzzyy/actbreak
cd actbreak
pip install -e .
```

Once it's on PyPI: `pipx install actbreak`.

Requires `act` on PATH, and one of Docker or Podman.

## Usage

```
actbreak run <workflow.yml> --break-before <step>
actbreak run <workflow.yml> --break-after <step>
actbreak run <workflow.yml> --break-on-failure

actbreak resume
actbreak clean
```

`<workflow.yml>` is either a path to a workflow file, or a bare name looked up
under `.github/workflows/`.

A step selector is either a step's `name:` value, or `<job>:<index>` to select
by zero-based position (use this for steps with no `name:`).

### `run` flags

| Flag | Meaning |
|---|---|
| `--break-before STEP` | pause immediately before `STEP` runs |
| `--break-after STEP` | pause immediately after `STEP` runs |
| `--break-on-failure` | if `act` exits nonzero, attach to the last job container for post-mortem |
| `--job JOB` | disambiguate a multi-job workflow |
| `--runtime {docker,podman,auto}` | container runtime to use (default: auto-detect) |
| `--no-attach` | don't exec a shell automatically; print the attach command and hold |
| `--act-arg ARG` | extra argument passed through to `act` (repeatable) |
| `-v`, `--verbose` | print the injection/act commands being run |

### Examples

```
actbreak run ci.yml --break-before "Run tests"
actbreak run ci.yml --job build --break-before build:2
actbreak run ci.yml --break-after "Build" --no-attach
actbreak run ci.yml --break-on-failure
```

## How it works

1. Finds the target workflow and, using the given job/step selector, resolves
   an exact step in it.
2. Copies the workflow to a temp file and splices a synthetic step in
   immediately before or after the target, using line-based text injection
   (never a YAML parse-and-re-serialize round trip; see below for why).
3. The injected step drops a sentinel file (`/tmp/actbreak/hold`) and blocks
   on it inside the container.
4. Runs `act -W <temp copy> --reuse` so the container stays alive after the
   run "finishes" (i.e. hangs at the hold).
5. Polls `docker ps` / `podman ps` for the job's container, then for the
   sentinel file, to know the breakpoint has been hit.
6. Execs an interactive shell into the container. Exiting the shell (or
   running `actbreak resume`) deletes the sentinel and lets the job continue.

### Why not just parse the YAML?

Because round-tripping a workflow through a generic YAML library corrupts it.
PyYAML's default loader coerces an unquoted `on:` key to the boolean `True`
under YAML 1.1 rules, and any generic dumper throws away comments, quoting
style, and anchors. actbreak never deserializes the file. It scans for
`jobs:`, then the target job, then its `steps:` list, using indentation alone,
and splices in new lines at the right point. Every other byte in the file is
untouched.

## Development

```
pip install -e ".[dev]"
python -m unittest
pytest
```

The `integration` pytest marker (`pytest -m integration`) runs a real
`act` + Docker/Podman end-to-end test; it's auto-skipped unless both are on
PATH, which in practice means it only runs in CI.

## License

Prosperity Public License 3.0.0 — free for noncommercial use, thirty-day trial for commercial use. See [LICENSE](LICENSE). Contributions come in under the Blue Oak Model License; see [CONTRIBUTING.md](CONTRIBUTING.md).
