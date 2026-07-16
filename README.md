# actbreak

[![CI](https://github.com/munzzyy/actbreak/actions/workflows/ci.yml/badge.svg)](https://github.com/munzzyy/actbreak/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/actbreak)](https://pypi.org/project/actbreak/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

actbreak is a local breakpoint debugger for GitHub Actions: pause a workflow mid-step
and get a real shell inside the still-running job container.

Built on [`act`](https://github.com/nektos/act), which runs GitHub Actions workflows
locally but has no way to pause mid-run on its own. actbreak injects the breakpoint,
waits for the job container to reach it, execs you in, and resumes the run when you're
done.

Zero runtime dependencies. Python 3.9+, stdlib only.

## Status

Early / v0.2.0. Core injection and selection logic is unit tested; the
run/resume/clean orchestration against real `act` + Docker/Podman is covered by
a CI integration test rather than exercised in every environment this ships to.

## Install

```
pipx install actbreak
```

Or from a clone, since it's stdlib-only:

```
git clone https://github.com/munzzyy/actbreak
cd actbreak
pip install -e .
```

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

### Shell completions

`actbreak --completions bash` (or `zsh`) prints a completion script built
from the argparse parser, so new flags show up without touching it:

```bash
# bash
source <(actbreak --completions bash)

# zsh
source <(actbreak --completions zsh)
```

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

## Limitations

- The breakpoint step needs a real shell in the job container: it runs `sh`
  with `mkdir`, `printf`, and `sleep`. A `scratch` or distroless image without
  those won't hold at the breakpoint.
- `act --reuse` keeps the job container alive so you can attach to it. actbreak
  reaps that container once the run finishes cleanly (resumed to the end, the
  breakpoint never hit, or a `--break-on-failure` run that passed), so a normal
  run doesn't leave a stopped container behind. The one it keeps on purpose is
  `--no-attach`, which parks the container for `actbreak resume` to pick up
  later — clear those with `actbreak clean`.

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

MIT — free to use, change, and ship, commercial or not. See [LICENSE](LICENSE).

## Support

If actbreak saved you a round of push-and-pray debugging, [sponsoring](https://github.com/sponsors/munzzyy) is what keeps it maintained.