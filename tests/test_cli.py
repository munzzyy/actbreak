"""CLI argument parsing tests. These stub out actbreak.session so no
subprocess/act/docker calls ever happen -- only argparse wiring is tested."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from actbreak.cli import build_parser, main


class ParserTests(unittest.TestCase):
    def test_run_requires_a_break_flag(self):
        with self.assertRaises(SystemExit):
            main(["run", "ci.yml"])

    def test_break_before_and_break_after_can_be_combined(self):
        # No longer mutually exclusive: several breakpoints, mixed, is the
        # whole point of stepping through more than one in one run.
        parser = build_parser()
        args = parser.parse_args(["run", "ci.yml", "--break-before", "a", "--break-after", "b"])
        self.assertEqual(args.breakpoints, [("before", "a"), ("after", "b")])

    def test_break_before_and_break_after_are_repeatable_and_ordered(self):
        parser = build_parser()
        args = parser.parse_args(
            ["run", "ci.yml", "--break-before", "a", "--break-after", "b", "--break-before", "c"]
        )
        self.assertEqual(args.breakpoints, [("before", "a"), ("after", "b"), ("before", "c")])

    def test_break_before_parses(self):
        parser = build_parser()
        args = parser.parse_args(["run", "ci.yml", "--break-before", "Run tests"])
        self.assertEqual(args.workflow, "ci.yml")
        self.assertEqual(args.breakpoints, [("before", "Run tests")])
        self.assertFalse(args.break_on_failure)
        self.assertIsNone(args.job)
        self.assertEqual(args.runtime, "auto")
        self.assertFalse(args.no_attach)
        self.assertIsNone(args.shell)
        self.assertEqual(args.act_arg, [])
        self.assertFalse(args.verbose)

    def test_all_flags_parse_together(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "run",
                "ci.yml",
                "--break-after",
                "build:2",
                "--break-on-failure",
                "--job",
                "build",
                "--runtime",
                "podman",
                "--no-attach",
                "--shell",
                "zsh",
                "--act-arg=--pull=false",
                "--act-arg=-P",
                "-v",
            ]
        )
        self.assertEqual(args.breakpoints, [("after", "build:2")])
        self.assertTrue(args.break_on_failure)
        self.assertEqual(args.job, "build")
        self.assertEqual(args.runtime, "podman")
        self.assertTrue(args.no_attach)
        self.assertEqual(args.shell, "zsh")
        self.assertEqual(args.act_arg, ["--pull=false", "-P"])
        self.assertTrue(args.verbose)

    def test_invalid_runtime_choice_rejected(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "ci.yml", "--break-on-failure", "--runtime", "vmware"])

    def test_version_flag(self):
        parser = build_parser()
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def _completions(self, shell):
        parser = build_parser()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                parser.parse_args(["--completions", shell])
        self.assertEqual(ctx.exception.code, 0)
        return buf.getvalue()

    def test_completions_bash_covers_parser(self):
        out = self._completions("bash")
        self.assertIn("_actbreak() {", out)
        for token in ("run", "resume", "clean", "init-vscode", "--version", "--completions",
                      "--break-before", "--break-after", "--break-on-failure",
                      "--job", "--runtime", "--no-attach", "--shell", "--act-arg",
                      "-v", "--verbose"):
            self.assertIn(token, out)

    def test_completions_zsh_covers_parser(self):
        out = self._completions("zsh")
        self.assertIn("#compdef actbreak", out)
        for token in ('"run"', '"resume"', '"clean"', '"init-vscode"',
                      "'--version[", "'--completions[", "'*--break-before[",
                      "'*--break-after[", "'--break-on-failure[", "'--job[",
                      "'--runtime[", "'--no-attach[", "'--shell[", "--act-arg[", "'-v[",
                      "'--verbose["):
            self.assertIn(token, out)

    def test_completions_zsh_registers_itself_when_sourced(self):
        # The README tells you to `source <(actbreak --completions zsh)`.
        # `#compdef` alone only works from $fpath, and calling _actbreak at the
        # end runs _arguments outside a completion context, which errors.
        out = self._completions("zsh")
        self.assertIn("#compdef actbreak", out)
        self.assertIn("compdef _actbreak actbreak", out)
        self.assertNotIn('_actbreak "$@"', out)

    def test_completions_zsh_flags_that_take_a_value_say_so(self):
        out = self._completions("zsh")
        # A value-taking flag needs a `:metavar:` tail or zsh offers the next
        # flag where the value belongs.
        self.assertIn("'*--break-before[pause immediately before STEP runs", out)
        self.assertIn("]:STEP:'", out)
        self.assertIn("]:JOB:'", out)
        # Choices become a completable value list.
        self.assertIn(":RUNTIME:(docker podman auto)'", out)
        # --act-arg is repeatable, so it needs the `*` prefix.
        self.assertIn("'*--act-arg[", out)
        # A bare switch must not claim to take one.
        self.assertNotIn("--no-attach[dont exec a shell automatically; print the attach "
                         "command and hold, then exit]:", out)

    def test_completions_zsh_descriptions_cannot_break_the_spec(self):
        # `_arguments` specs are single-quoted and their description ends at
        # the first `]`, so neither character may survive into one.
        out = self._completions("zsh")
        for line in out.splitlines():
            stripped = line.strip()
            if not stripped.startswith("'") and not stripped.startswith("'*"):
                continue
            body = stripped.rstrip("\\").strip().strip("'")
            if "[" not in body:
                continue
            description = body.split("[", 1)[1].split("]", 1)[0]
            self.assertNotIn("'", description, line)
            self.assertNotIn("[", description, line)

    def test_missing_command_is_an_error(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_resume_and_clean_take_no_positional_args(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["resume"]).command, "resume")
        self.assertEqual(parser.parse_args(["clean"]).command, "clean")

    def test_init_vscode_takes_no_positional_args(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["init-vscode"]).command, "init-vscode")

    def test_list_takes_no_positional_args(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["list"]).command, "list")

    def test_completions_cover_the_list_command(self):
        self.assertIn("list", self._completions("bash"))
        self.assertIn('"list"', self._completions("zsh"))

    def test_steps_takes_a_workflow_and_an_optional_job(self):
        parser = build_parser()
        args = parser.parse_args(["steps", "ci.yml"])
        self.assertEqual(args.command, "steps")
        self.assertEqual(args.workflow, "ci.yml")
        self.assertIsNone(args.job)
        self.assertEqual(parser.parse_args(["steps", "ci.yml", "--job", "build"]).job, "build")

    def test_steps_requires_a_workflow(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["steps"])

    def test_completions_cover_the_steps_command(self):
        self.assertIn("steps", self._completions("bash"))
        self.assertIn("steps", self._completions("zsh"))


class MainDispatchTests(unittest.TestCase):
    def test_main_dispatches_run_to_session(self):
        with mock.patch("actbreak.session.cmd_run", return_value=0) as fake_run:
            rc = main(["run", "ci.yml", "--break-before", "Build"])
        self.assertEqual(rc, 0)
        fake_run.assert_called_once()

    def test_main_dispatches_resume(self):
        with mock.patch("actbreak.session.cmd_resume", return_value=0) as fake:
            rc = main(["resume"])
        self.assertEqual(rc, 0)
        fake.assert_called_once()

    def test_main_dispatches_clean(self):
        with mock.patch("actbreak.session.cmd_clean", return_value=0) as fake:
            rc = main(["clean"])
        self.assertEqual(rc, 0)
        fake.assert_called_once()

    def test_main_dispatches_list(self):
        with mock.patch("actbreak.session.cmd_list", return_value=0) as fake:
            rc = main(["list"])
        self.assertEqual(rc, 0)
        fake.assert_called_once()

    def test_main_dispatches_steps(self):
        with mock.patch("actbreak.session.cmd_steps", return_value=0) as fake:
            rc = main(["steps", "ci.yml"])
        self.assertEqual(rc, 0)
        fake.assert_called_once()

    def test_main_dispatches_init_vscode(self):
        with mock.patch("actbreak.vscode_tasks.cmd_init_vscode", return_value=0) as fake:
            rc = main(["init-vscode"])
        self.assertEqual(rc, 0)
        fake.assert_called_once()

    def test_main_converts_actbreak_error_to_exit_code_1(self):
        from actbreak.errors import ActbreakError

        with mock.patch("actbreak.session.cmd_run", side_effect=ActbreakError("boom")):
            rc = main(["run", "ci.yml", "--break-before", "Build"])
        self.assertEqual(rc, 1)

    def test_main_converts_keyboard_interrupt_to_130(self):
        with mock.patch("actbreak.session.cmd_run", side_effect=KeyboardInterrupt):
            rc = main(["run", "ci.yml", "--break-before", "Build"])
        self.assertEqual(rc, 130)

    def test_run_without_any_break_flag_errors_before_touching_session(self):
        with mock.patch("actbreak.session.cmd_run") as fake_run:
            with self.assertRaises(SystemExit) as ctx:
                main(["run", "ci.yml"])
        self.assertEqual(ctx.exception.code, 2)
        fake_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
