# actbreak

[![CI](https://github.com/munzzyy/actbreak/actions/workflows/ci.yml/badge.svg)](https://github.com/munzzyy/actbreak/actions/workflows/ci.yml)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

actbreak is a local breakpoint debugger for GitHub Actions: pause a workflow mid-step
and get a real shell inside the still-running job container.

Built on [`act`](https://github.com/nektos/act), which runs GitHub Actions workflows
locally but has no way to pause mid-run on its own. actbreak injects the breakpoint,
waits for the job container to reach it, execs you in, and resumes the run when you're
done.

Zero runtime dependencies. Python 3.9+, stdlib only.

## Status

Early / v0.3.0, which isn't released yet. [CHANGELOG.md](CHANGELOG.md) lists
what changed. The injection, selection and session logic is unit tested
against fakes. One CI job also runs real breakpoints through real `act` and
Docker. One case injects the hold, waits for the job container to reach it,
and releases it. Another runs `actbreak run --matrix ... --no-attach` and
`actbreak clean` as commands against a two-leg matrix. `resume` and the
rest of the commands are covered by the unit tests only.

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
completions, and it still carries the old Prosperity license.
The 0.1.1 and 0.2.0 uploads failed, and the next one to go up will be 0.3.0,
so install from git until the PyPI page says 0.3.0.

Requires `act` on PATH, and one of Docker or Podman.

## Usage

```
actbreak run <workflow.yml> --break-before <step>
actbreak run <workflow.yml> --break-after <step>
actbreak run <workflow.yml> --break-on-failure

actbreak steps <workflow.yml>

actbreak resume [SESSION]
actbreak clean [SESSION]
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
| `--break-before STEP` | pause immediately before `STEP` runs (repeatable) |
| `--break-after STEP` | pause immediately after `STEP` runs, whether it passed or failed (repeatable) |
| `--break-on-failure` | if `act` exits nonzero, attach to the last job container for post-mortem |
| `--job JOB` | disambiguate a multi-job workflow |
| `--runtime {docker,podman,auto}` | container runtime to use (default: auto-detect) |
| `--no-attach` | don't exec a shell automatically; print the attach command and hold |
| `--shell SHELL` | shell to attach with, e.g. `zsh` or `'bash -l'` (default: try `sh`, then `bash`) |
| `--matrix KEY:VALUE` | run only the matrix leg where `KEY` is `VALUE`, passed to act's own `--matrix` (repeatable, once per matrix key) |
| `--timeout SECONDS` | how long to wait for each breakpoint before stopping `act` and removing its container (default: 1800; `0` means no deadline, wait as long as `act` runs) |
| `--act-arg ARG` | extra argument passed through to `act` (repeatable) |
| `-v`, `--verbose` | print the injection/act commands being run |

### Multiple breakpoints

`--break-before` and `--break-after` are both repeatable and can be mixed, so
one `run` can step through several points instead of you re-running it fresh
for each one:

```
actbreak run ci.yml --break-before "Install deps" --break-after "Build"
```

They're hit in the order the job actually reaches them, which is by their
position in the file, not the order you passed them on the command line.
Attaching and exiting the shell (or running `actbreak resume` on a
`--no-attach` session) moves you to the next one; once you resume past the
last one the job runs to completion like normal. All of a run's breakpoints
have to resolve to the same job, since `actbreak run` only ever debugs one.

### Parked sessions

```
actbreak list
```

Shows the debug sessions a `run --no-attach` (or a `resume`/`clean` that
couldn't finish) left parked, one per line, with each one's live container
status read from `docker ps` / `podman ps`:

```
actbreak: 3 parked debug sessions:
  act-CI-build [running] -- job 'build', step 'Run tests' (before), held for 5m -- /repo/.github/workflows/ci.yml
  act-CI-test [gone] -- job 'test', step 'Build' (after), held for 3h -- /repo/.github/workflows/ci.yml
  act-CI-lint [running] -- job 'lint', post-mortem after act exited 1, held for 2m -- /repo/.github/workflows/ci.yml
```

`running` is still held and attachable; `stopped` and `gone` are orphans to
clear with `actbreak clean`. The `held for` age is how long ago the session
was parked, so you can spot the one that's been sitting for hours instead of
minutes. With nothing parked it just says so.

A `--break-on-failure --no-attach` run that fails parks its post-mortem
container the same way. act has already exited by then and there is no hold
to drop, so `resume` leaves it alone and `clean` removes it.

`actbreak clean` removes every parked container and its temp files. If a
removal fails, it keeps that session, says so, and exits 1, so the next
`clean` can try again. A session whose container is already gone is just
dropped.

With several sessions parked, `resume` and `clean` act on all of them by
default. Name one to act on just that session: its container name as `list`
prints it, or any prefix of it that only one session has.

```
actbreak resume act-CI-test
actbreak clean act-CI-b
```

A prefix that matches more than one session, or none, is an error that lists
what is parked. `clean` with a name skips its sweep for untracked
containers, since that sweep would take the other parked sessions too.

While a job is parked, `actbreak run` won't start that job again. The new
run would stop at the old hold on its first check instead of its own, and
dropping either hold would drop both. The error names the container in the
way so you can resume or clean it first. A container that still holds a
breakpoint but has no recorded session blocks the run the same way.

`actbreak resume` drops the hold and then waits for the job to finish, so it
can reap the container instead of leaving it behind. That wait is the rest
of your workflow, so it can sit there for a while. Ctrl-C stops
waiting and leaves the job running; `actbreak clean` reaps it afterwards. A
session parked from a multi-breakpoint `run --no-attach` instead re-parks at
the next breakpoint if the job reaches it before finishing, so `resume` steps
through them one at a time the same way attaching would.

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
actbreak run ci.yml --break-before "Install deps" --break-after "Build"
actbreak run ci.yml --break-on-failure
actbreak run ci.yml --break-before "Run tests" --shell zsh
actbreak run ci.yml --break-before "Run tests" --matrix os:ubuntu-latest --matrix python:3.12
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

- A matrix job needs `--matrix` to pick one leg. act runs one container per
  leg and they all carry the same job id, so actbreak can't tell them apart
  and refuses to guess. `--matrix KEY:VALUE`, given once for each matrix key,
  is passed straight to act so only that leg runs. Without it, actbreak
  names the containers and prints the `docker exec` / `podman exec` command
  for each, so you can attach to the leg you want by hand.
- The breakpoint step needs a real shell in the job container: it runs `sh`
  with `mkdir`, `printf`, and `sleep`. A `scratch` or distroless image without
  those won't hold at the breakpoint.
- act names a job's container after the job's `name:` when it has one. A
  `name:` with an expression in it, like `Tests (${{ matrix.os }})`, is
  filled in by act first, so actbreak can't predict the container's name.
  `run` warns about it up front, and may wait until `--timeout` runs out.
  Take the expression out of `name:` while you debug that job.
- Attaching tries `sh`, then `bash`, unless `--shell` says otherwise. An image
  that needs something else (`zsh`, or a login shell via `bash -l`) needs
  `--shell` set explicitly.
- `act --reuse` keeps the job container alive so you can attach to it, and it
  stays running after act exits. actbreak reaps that container once the run
  finishes cleanly (resumed to the end, the breakpoint never hit, or a
  `--break-on-failure` run that passed), so a normal run doesn't leave one
  behind. `resume` knows the job is done when act's process exits. It can't
  check that on Windows, so there it waits out its 30-minute limit; Ctrl-C
  and `actbreak clean` end it sooner. The ones it keeps on purpose
  are recorded so `actbreak list` shows them and `actbreak clean` removes them:
  a `--no-attach` breakpoint, parked for `actbreak resume` to pick up later, a
  `--break-on-failure --no-attach` post-mortem container, and the containers a
  failed `--break-on-failure` run leaves when several jobs are still alive and
  it has no single one to attach to.

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

## Roadmap

What is left needs the maintainer, or people running actbreak on their own
workflows.

- Cut 0.3.0 and get it onto PyPI. Everything under 0.3.0 in
  [CHANGELOG.md](CHANGELOG.md) is done, but there is no tag yet. PyPI
  rejected the earlier uploads because the project there does not trust the
  release workflow yet. Adding that is a setting on pypi.org behind the
  maintainer's login. Until then, install from git as [Install](#install)
  says.
- Reports from real projects. Most commands are only tested against fakes
  so far (see [Status](#status)). `resume`, `clean` and `--break-on-failure`
  depend on how act names its containers and what `act --reuse` leaves
  running, and only real act can settle that. Jobs that set their own
  `name:` are the case most worth trying. If something misbehaves, an issue
  with the workflow and your act version helps the most.

## License

[GPL-3.0-or-later](LICENSE). You can use, study, change and share it. If you distribute a copy or a modified version, it has to stay under the GPL and come with its source. Releases up to and including v0.2.0 were under the Prosperity Public License 3.0.0. The code sat under MIT on main for a while after that, but no release was cut under MIT.

## Support

If actbreak saved you a round of push-and-pray debugging, [sponsoring](https://github.com/sponsors/munzzyy) is what keeps it maintained.