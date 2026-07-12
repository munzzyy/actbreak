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

    def test_break_before_and_break_after_are_mutually_exclusive(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "ci.yml", "--break-before", "a", "--break-after", "b"])

    def test_break_before_parses(self):
        parser = build_parser()
        args = parser.parse_args(["run", "ci.yml", "--break-before", "Run tests"])
        self.assertEqual(args.workflow, "ci.yml")
        self.assertEqual(args.break_before, "Run tests")
        self.assertIsNone(args.break_after)
        self.assertFalse(args.break_on_failure)
        self.assertIsNone(args.job)
        self.assertEqual(args.runtime, "auto")
        self.assertFalse(args.no_attach)
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
                "--act-arg=--pull=false",
                "--act-arg=-P",
                "-v",
            ]
        )
        self.assertEqual(args.break_after, "build:2")
        self.assertTrue(args.break_on_failure)
        self.assertEqual(args.job, "build")
        self.assertEqual(args.runtime, "podman")
        self.assertTrue(args.no_attach)
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
        for token in ("run", "resume", "clean", "--version", "--completions",
                      "--break-before", "--break-after", "--break-on-failure",
                      "--job", "--runtime", "--no-attach", "--act-arg",
                      "-v", "--verbose"):
            self.assertIn(token, out)

    def test_completions_zsh_covers_parser(self):
        out = self._completions("zsh")
        self.assertIn("#compdef actbreak", out)
        for token in ('"run"', '"resume"', '"clean"', '"--version"',
                      '"--completions"', '"--break-before"', '"--break-after"',
                      '"--break-on-failure"', '"--job"', '"--runtime"',
                      '"--no-attach"', '"--act-arg"', '"-v"', '"--verbose"'):
            self.assertIn(token, out)

    def test_missing_command_is_an_error(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_resume_and_clean_take_no_positional_args(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["resume"]).command, "resume")
        self.assertEqual(parser.parse_args(["clean"]).command, "clean")


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
