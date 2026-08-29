"""Command-line interface for actbreak."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .errors import ActbreakError

PROG = "actbreak"

EPILOG = """\
examples:
  actbreak run ci.yml --break-before "Run tests"
  actbreak run ci.yml --job build --break-before build:2
  actbreak run ci.yml --break-after "Build" --job build --no-attach
  actbreak run ci.yml --break-before "Install deps" --break-after "Build"
  actbreak run ci.yml --break-on-failure
  actbreak run ci.yml --break-before "Run tests" --shell bash
  actbreak steps ci.yml
  actbreak resume
  actbreak clean
  actbreak list
  actbreak init-vscode

step selectors:
  a step name (matched against the step's `name:` in the workflow), or
  "<job>:<index>" to select by zero-based position, e.g. "build:0" -- use
  this for steps that have no `name:`. `actbreak steps <workflow>` prints
  every selector a workflow offers.

notes:
  --break-before and --break-after are both repeatable and can be mixed, to
  set several breakpoints in one run and step through them in the order the
  job actually reaches them (which is by file position, not the order you
  passed them in). All of a single run's breakpoints must resolve to the
  same job.

  --act-arg is passed straight through to `act`, repeatably. If the value
  itself starts with '-' (e.g. -P or --pull=false), use the "=" form so
  argparse doesn't mistake it for one of actbreak's own flags:
  --act-arg=-P, --act-arg=--pull=false.
"""


class BreakpointAction(argparse.Action):
    """Append a (position, selector) pair to a single shared list, so
    --break-before and --break-after can be mixed and repeated to set
    several breakpoints in one run instead of exactly one. `const` carries
    which flag this is ('before' or 'after'); `repeatable` is read by
    completions.py so the generated zsh script marks these flags as
    repeatable the same way it does for --act-arg."""

    repeatable = True

    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest, None)
        if items is None:
            items = []
        items.append((self.const, values))
        setattr(namespace, self.dest, items)


class PrintCompletionsAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        from .completions import generate_bash, generate_zsh

        if values == "bash":
            print(generate_bash(parser), end="")
        elif values == "zsh":
            print(generate_zsh(parser), end="")
        parser.exit(0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="A local breakpoint debugger for GitHub Actions workflows, wrapping `act`.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--completions",
        choices=["bash", "zsh"],
        action=PrintCompletionsAction,
        help="print shell completion script",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser(
        "run",
        help="run a workflow under act with a breakpoint",
        description="Inject a breakpoint into a workflow and run it under act.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_p.add_argument(
        "workflow",
        help="workflow file to run (a path, or a bare name looked up under .github/workflows)",
    )
    run_p.add_argument(
        "--break-before",
        action=BreakpointAction,
        const="before",
        dest="breakpoints",
        metavar="STEP",
        help="pause immediately before STEP runs (name, or '<job>:<index>'); repeatable",
    )
    run_p.add_argument(
        "--break-after",
        action=BreakpointAction,
        const="after",
        dest="breakpoints",
        metavar="STEP",
        help="pause immediately after STEP runs (name, or '<job>:<index>'); repeatable",
    )
    run_p.add_argument(
        "--break-on-failure",
        action="store_true",
        help="if act exits nonzero, attach to the last job container for post-mortem inspection",
    )
    run_p.add_argument("--job", metavar="JOB", help="job id, to disambiguate a multi-job workflow")
    run_p.add_argument(
        "--runtime",
        choices=("docker", "podman", "auto"),
        default="auto",
        help="container runtime to use (default: auto-detect)",
    )
    run_p.add_argument(
        "--no-attach",
        action="store_true",
        help="don't exec a shell automatically; print the attach command and hold, then exit",
    )
    run_p.add_argument(
        "--shell",
        metavar="SHELL",
        help="shell to attach with, e.g. zsh or 'bash -l' (default: try sh, then bash)",
    )
    run_p.add_argument(
        "--act-arg",
        action="append",
        metavar="ARG",
        default=[],
        help="extra argument to pass through to act (repeatable)",
    )
    run_p.add_argument("-v", "--verbose", action="store_true", help="print the act/injection commands being run")

    sub.add_parser("resume", help="release an active breakpoint hold")
    sub.add_parser("clean", help="kill leftover held containers and temp dirs")
    sub.add_parser(
        "list",
        help="show parked debug sessions and each one's container status",
        description=(
            "List the debug sessions left parked by 'run --no-attach' (or a "
            "resume/clean that couldn't finish), with each one's live container "
            "status (running, stopped, or gone) so you can see what's still held "
            "and reap orphans."
        ),
    )

    steps_p = sub.add_parser(
        "steps",
        help="list the selectable steps in a workflow",
        description=(
            "Print every step actbreak can break on in a workflow, one per "
            "line, as '<job>:<index>  <name>'. The selector comes first so it "
            "can be pasted straight into --break-before / --break-after."
        ),
    )
    steps_p.add_argument(
        "workflow",
        help="workflow file (a path, or a bare name looked up under .github/workflows)",
    )
    steps_p.add_argument("--job", metavar="JOB", help="only show this job's steps")

    sub.add_parser(
        "init-vscode",
        help="generate a VS Code task per (workflow, job, step) under .github/workflows/",
        description=(
            "Write .vscode/tasks.json with one task per step, each running the real "
            "`actbreak run --break-before <job>:<index>` command for it."
        ),
    )

    return parser


def _validate_run_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    args.breakpoints = args.breakpoints or []
    if not (args.breakpoints or args.break_on_failure):
        parser.error("run: give at least one of --break-before, --break-after, or --break-on-failure")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Import lazily so `actbreak --version`/`--help` never need to import
    # subprocess-heavy session machinery.
    from . import session

    try:
        if args.command == "run":
            _validate_run_args(parser, args)
            return session.cmd_run(args)
        if args.command == "resume":
            return session.cmd_resume(args)
        if args.command == "clean":
            return session.cmd_clean(args)
        if args.command == "list":
            return session.cmd_list(args)
        if args.command == "steps":
            return session.cmd_steps(args)
        if args.command == "init-vscode":
            from . import vscode_tasks

            return vscode_tasks.cmd_init_vscode(args)
        parser.error(f"unknown command: {args.command}")
        return 2  # unreachable, parser.error exits
    except ActbreakError as e:
        print(f"actbreak: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nactbreak: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
