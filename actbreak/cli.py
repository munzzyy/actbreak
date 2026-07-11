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
  actbreak run ci.yml --break-on-failure
  actbreak resume
  actbreak clean

step selectors:
  a step name (matched against the step's `name:` in the workflow), or
  "<job>:<index>" to select by zero-based position, e.g. "build:0" -- use
  this for steps that have no `name:`.

notes:
  --act-arg is passed straight through to `act`, repeatably. If the value
  itself starts with '-' (e.g. -P or --pull=false), use the "=" form so
  argparse doesn't mistake it for one of actbreak's own flags:
  --act-arg=-P, --act-arg=--pull=false.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="A local breakpoint debugger for GitHub Actions workflows, wrapping `act`.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

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
    break_group = run_p.add_mutually_exclusive_group()
    break_group.add_argument(
        "--break-before",
        metavar="STEP",
        help="pause immediately before STEP runs (name, or '<job>:<index>')",
    )
    break_group.add_argument(
        "--break-after",
        metavar="STEP",
        help="pause immediately after STEP runs (name, or '<job>:<index>')",
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
        "--act-arg",
        action="append",
        metavar="ARG",
        default=[],
        help="extra argument to pass through to act (repeatable)",
    )
    run_p.add_argument("-v", "--verbose", action="store_true", help="print the act/injection commands being run")

    sub.add_parser("resume", help="release an active breakpoint hold")
    sub.add_parser("clean", help="kill leftover held containers and temp dirs")

    return parser


def _validate_run_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not (args.break_before or args.break_after or args.break_on_failure):
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
