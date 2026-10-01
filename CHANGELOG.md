# Changelog

## 0.3.0 (unreleased)

Licensed GPL-3.0-or-later from this release on. Earlier releases were under
the Prosperity Public License 3.0.0, as the [README](README.md#license) says.

New:

- `actbreak list` shows the sessions a `run --no-attach` left parked. Each
  line has the container's live status and how long it has been held.
- `actbreak steps <workflow>` prints every step selector a workflow offers
  in a form you can paste into `--break-before` or `--break-after`.
- `actbreak init-vscode` writes one VS Code task per workflow step.
- `--break-before` and `--break-after` can be repeated and mixed to stop at
  several points in one run. `resume` steps through them one at a time.
- `--shell` picks the shell to attach with: `zsh` or `'bash -l'` for example.
- `actbreak resume SESSION` and `actbreak clean SESSION` act on one parked
  session, named by its container or a unique prefix of it.
- `run --matrix KEY:VALUE` runs a single leg of a matrix job through act's
  own `--matrix`, so a matrix job can be debugged at all.
- `run --timeout SECONDS` sets how long to wait for each breakpoint. The
  default is still 30 minutes, and 0 waits as long as act runs.

Fixes:

- The breakpoint step carries `if: always()`, so it still holds after an
  earlier step failed.
- `resume` says what it's waiting on and lets Ctrl-C stop the wait without
  touching the job. It also says when it gives up waiting, instead of
  exiting quietly with the session still parked.
- `resume` saves its progress after every session. An interrupted resume
  no longer leaves finished sessions in the state file.
- `clean` keeps a session it failed to remove and exits 1. It used to drop
  the session and exit 0.
- A failed `--break-on-failure --no-attach` run records its post-mortem
  container, so `list` shows it and `clean` removes it.
- A session parked with `--shell` remembers it for the next attach command.
- Exiting the breakpoint shell with status 127 (a mistyped command, then
  `exit`) no longer opens a second shell.
- A matrix job's ambiguity error names each leg's container and prints the
  exec command for it.
- The zsh completion works when sourced. Flags that take a value say so.
- `--break-on-failure` never attaches to or removes another workflow's
  parked container, and it reaps every job's container in a multi-job
  workflow.
- `resume` on a stopped container keeps the session for `clean` instead of
  reporting success.
- Ctrl-C during a `--break-on-failure` run without `--job` removes the
  job container along with act.
- Workflows containing U+2028, U+2029 or U+0085 are rejected with the line
  number instead of being split into the wrong steps.

Project:

- License metadata uses the PEP 639 form.
- The release workflow refuses to publish when the version and tag disagree.
- CI installs act from a pinned, checksummed release and tests Python 3.14.

## 0.2.0 (2026-07-15)

Under the Prosperity Public License 3.0.0. The PyPI upload failed, so this
version was never on PyPI.

- `actbreak --completions bash|zsh` prints a completion script generated
  from the argument parser (#1).
- After a clean run, the job container `act --reuse` leaves behind is
  removed instead of leaking.
- Flush-left `steps:` lists, a `jobs:` line with trailing whitespace, tabs
  inside `run: |` blocks, and folded or literal step names all parse now.
- A step whose name looks like `job:index` (say `deploy:2`) can be
  selected by name.
- An ambiguous container match fails at once instead of after the full
  wait.
- When the wait gives up, act is stopped and its container removed instead
  of being left running with nothing recorded.
- `resume` keeps a session it failed to resume.
- The workflows pin actions to commit SHAs. A security policy was added.

## 0.1.1 (2026-07-11)

Under the Prosperity Public License 3.0.0. The PyPI upload failed, so this
version was never on PyPI.

- The breakpoint banner uses `printf` and folds newlines in step names, so
  a name can't break out of the injected `run:` block.
- Output from the in-container `test` and `rm` calls is captured instead of
  going to the terminal.
- Job names match whole tokens in container names, so `test` no longer
  matches `latest` and `a` no longer matches everything.

## 0.1.0

First release, under the Prosperity Public License 3.0.0. It is the only
version on PyPI so far.
