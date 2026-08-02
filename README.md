# actbreak

[![CI](https://github.com/munzzyy/actbreak/actions/workflows/ci.yml/badge.svg)](https://github.com/munzzyy/actbreak/actions/workflows/ci.yml)
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
pipx install git+https://github.com/munzzyy/actbreak
```

Or from a clone, since it's stdlib-only:

```
git clone https://github.com/munzzyy/actbreak
cd actbreak
pip install -e .
```

Don't install actbreak from PyPI yet. The listing there is stuck at 0.1.0,
which predates `actbreak list`, `actbreak steps`, `init-vscode`, and shell
completions, and it still carries the old Prosperity license instead of MIT.
The 0.2.0 publish failed and hasn't been retried, so install from git until
the PyPI page says 0.2.0.

Requires `act` on PATH, and one of Docker or Podman.

## Usage

```
actbreak run <workflow.yml> --break-before <step>
actbreak run <workflow.yml> --break-after <step>
actbreak run <workflow.yml> --break-on-failure

actbreak steps <workflow.yml>

actbreak resume
actbreak clean
actbreak list

actbreak init-vscode
```

`<workflow.yml>` is either a path to a workflow file, or a bare name looked up
under `.github/workflows/`.

A step selector is either a step's `name:` value, or `<job>:<index>` to select
by zero-based position (use this for steps with no `name:`).

### Finding a step to break on

```
actbreak steps ci.yml
```

Prints every selector the workflow offers, selector first so you can paste one
straight into `--break-before` or `--break-after`:

```
build:
  build:0  Checkout
  build:1  Install deps
  build:2  Run tests
lint:
  lint:0  Checkout
  lint:1  (unnamed)
```

`--job JOB` narrows it to one job. Steps with no `name:` show as `(unnamed)`;
those are the ones you have to select by position.

### `run` flags

| Flag | Meaning |
|---|---|
| `--break-before STEP` | pause immediately before `STEP` runs |
| `--break-after STEP` | pause immediately after `STEP` runs, whether it passed or failed |
| `--break-on-failure` | if `act` exits nonzero, attach to the last job container for post-mortem |
| `--job JOB` | disambiguate a multi-job workflow |
| `--runtime {docker,podman,auto}` | container runtime to use (default: auto-detect) |
| `--no-attach` | don't exec a shell automatically; print the attach command and hold |
| `--act-arg ARG` | extra argument passed through to `act` (repeatable) |
| `-v`, `--verbose` | print the injection/act commands being run |

### Parked sessions

```
actbreak list
```

Shows the debug sessions a `run --no-attach` (or a `resume`/`clean` that
couldn't finish) left parked, one per line, with each one's live container
status read from `docker ps` / `podman ps`:

```
actbreak: 2 parked debug sessions:
  act-CI-build [running] -- job 'build', step 'Run tests' (before) -- /repo/.github/workflows/ci.yml
  act-CI-test [gone] -- job 'test', step 'Build' (after) -- /repo/.github/workflows/ci.yml
```

`running` is still held and attachable; `stopped` and `gone` are orphans to
clear with `actbreak clean`. With nothing parked it just says so.

### VS Code tasks

```
actbreak init-vscode
```

Scans every `.github/workflows/*.yml` (and `.yaml`) and writes one VS Code
task per step: `actbreak: <workflow> / <job> / <step>`, each running the real
`actbreak run <workflow> --break-before "<job>:<index>"` command in the
integrated terminal. Instead of typing the step selector by hand, open the
command palette (`Cmd/Ctrl+Shift+P` → "Tasks: Run Task") and pick the step.

Reruns are idempotent: a second `init-vscode` replaces only the tasks it
generated last time (matched by the `actbreak: ` label prefix) and leaves
every other task in `.vscode/tasks.json` untouched. If that file already has
`//` comments or a trailing comma (both legal in VS Code's own format, not in
plain JSON), it's left alone entirely and the generated tasks go to
`.vscode/actbreak-tasks.json` instead, for you to merge in by hand.

### Shell completions

`actbreak --completions bash` (or `zsh`) prints a completion script built
from the argparse parser, so new flags show up without touching it:

```bash
# bash
source <(actbreak --completions bash)

# zsh
source <(actbreak --completions zsh)
```

For zsh, the persistent version is a file in your `$fpath`, which is what
`#compdef` at the top of the script is for:

```bash
mkdir -p ~/.zfunc
actbreak --completions zsh > ~/.zfunc/_actbreak
# in ~/.zshrc, before compinit:
#   fpath=(~/.zfunc $fpath)
```

Either way you need `compinit` to have run, which most zsh setups (and
oh-my-zsh) already do.

### Examples

```
actbreak steps ci.yml
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
   on it inside the container. It carries `if: always()`, so the breakpoint
   still holds when an earlier step has already failed, which is the case
   you most want a shell for.
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
  later; clear those with `actbreak clean`.

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

MIT. Free to use, change, and ship, commercial or not. See [LICENSE](LICENSE).

## Support

If actbreak saved you a round of push-and-pray debugging, [sponsoring](https://github.com/sponsors/munzzyy) is what keeps it maintained.