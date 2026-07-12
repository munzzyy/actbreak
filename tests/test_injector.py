"""Tests for actbreak.injector -- the critical gate.

These assert that injecting a breakpoint never disturbs a single byte outside
the spliced-in step, across every shape of workflow file we expect to see in
the wild: quoted/unquoted/flow-style `on:`, comments at every nesting level,
matrix jobs, multiple jobs, CRLF line endings, unnamed steps, and step names
containing colons/quotes.
"""

from __future__ import annotations

import unittest

from actbreak import injector
from actbreak.errors import InjectionError
from actbreak.selector import resolve_selector

from .util import fixture_path

# build_hold_lines' output length is a function only of the fixed shell
# script it emits -- constant regardless of job/label/position/indent.
HOLD_LINE_COUNT = len(injector.build_hold_lines("j", "l", "before", 0, "\n"))


def load(name):
    text, has_bom = injector.read_workflow_text(fixture_path(name))
    lines = text.splitlines(keepends=True)
    jobs = injector.parse_workflow(lines)
    return text, lines, jobs, has_bom


def assert_exact_splice(text, lines, jobs, job, idx, position):
    """Inject, then verify every line outside the spliced-in block is
    byte-identical to the original -- the core correctness property."""
    step = jobs[job].steps[idx]
    insert_at = step.start if position == "before" else step.end
    result = injector.inject(lines, jobs, job, idx, position)

    remainder = result[:insert_at] + result[insert_at + HOLD_LINE_COUNT :]

    unterminated_last_line = (
        insert_at == len(lines) and bool(lines) and not lines[-1].endswith(("\n", "\r"))
    )
    if unterminated_last_line:
        newline = injector._detect_newline(text)
        assert remainder[:-1] == lines[:-1], "content before the appended-to line must be untouched"
        assert remainder[-1] == lines[-1] + newline, "only a newline should be appended to the last line"
    else:
        assert remainder == lines, "every line outside the injected block must be byte-identical"

    return result


class InjectorParsingTests(unittest.TestCase):
    def test_basic_parses_all_steps(self):
        _, _, jobs, _ = load("basic.yml")
        self.assertEqual(list(jobs.keys()), ["build"])
        names = [s.name for s in jobs["build"].steps]
        self.assertEqual(names, ["Checkout", "Install deps", "Run tests", None])

    def test_multi_job_parses_all_jobs(self):
        _, _, jobs, _ = load("multi_job.yml")
        self.assertEqual(set(jobs.keys()), {"lint", "build", "test"})
        self.assertEqual(len(jobs["lint"].steps), 2)
        self.assertEqual(len(jobs["build"].steps), 3)
        self.assertEqual(len(jobs["test"].steps), 3)

    def test_matrix_job_steps_parsed_normally(self):
        _, _, jobs, _ = load("matrix.yml")
        names = [s.name for s in jobs["test"].steps]
        self.assertEqual(
            names,
            ["Checkout", "Set up Python ${{ matrix.python-version }}", "Install", "Test"],
        )

    def test_unnamed_and_dash_alone_steps(self):
        _, _, jobs, _ = load("unnamed_steps.yml")
        names = [s.name for s in jobs["build"].steps]
        self.assertEqual(
            names,
            [None, None, "Run tests", None, "Trailing dash-alone step"],
        )

    def test_colon_and_quoted_names_decoded(self):
        _, _, jobs, _ = load("colon_names.yml")
        names = [s.name for s in jobs["build"].steps]
        self.assertEqual(names, ["Build: the app", "It's a test: quoted", "Deploy"])

    def test_comments_do_not_confuse_step_boundaries(self):
        _, _, jobs, _ = load("comments.yml")
        names = [s.name for s in jobs["build"].steps]
        self.assertEqual(names, ["Checkout", "Run tests"])

    def test_tabs_rejected_with_clear_error(self):
        with self.assertRaises(InjectionError) as ctx:
            load("tabs.yml")
        self.assertIn("tab", str(ctx.exception).lower())

    def test_no_top_level_jobs_key_rejected(self):
        lines = ["name: broken\n", "on: push\n"]
        with self.assertRaises(InjectionError):
            injector.parse_workflow(lines)


class InjectorSpliceTests(unittest.TestCase):
    def test_basic_break_before_and_after(self):
        text, lines, jobs, _ = load("basic.yml")
        job, idx = resolve_selector(jobs, "Run tests")
        assert_exact_splice(text, lines, jobs, job, idx, "before")
        assert_exact_splice(text, lines, jobs, job, idx, "after")

    def test_quoted_on_key_is_never_touched(self):
        text, lines, jobs, _ = load("quoted_on.yml")
        job, idx = resolve_selector(jobs, "Deploy")
        result = assert_exact_splice(text, lines, jobs, job, idx, "before")
        # The quoted "on": block, verbatim, must still be present.
        on_block = "".join(lines[3:12])  # '"on":' through the end of workflow_dispatch.inputs
        self.assertIn('"on":', on_block)
        self.assertIn(on_block, "".join(result))

    def test_flow_style_on_is_never_touched(self):
        text, lines, jobs, _ = load("flow_on.yml")
        job, idx = resolve_selector(jobs, "Test")
        result = assert_exact_splice(text, lines, jobs, job, idx, "before")
        self.assertIn("on: [push, pull_request]\n", "".join(result))

    def test_matrix_job_splice(self):
        text, lines, jobs, _ = load("matrix.yml")
        job, idx = resolve_selector(jobs, "Test")
        result = assert_exact_splice(text, lines, jobs, job, idx, "after")
        # The matrix definition must survive untouched.
        self.assertIn('python-version: ["3.10", "3.11", "3.12"]', "".join(result))

    def test_multi_job_splice_only_touches_target_job(self):
        text, lines, jobs, _ = load("multi_job.yml")
        job, idx = resolve_selector(jobs, "build:1")
        result = assert_exact_splice(text, lines, jobs, job, idx, "before")
        result_text = "".join(result)
        # Other jobs' step counts must be unaffected.
        result_lines = result_text.splitlines(keepends=True)
        result_jobs = injector.parse_workflow(result_lines)
        self.assertEqual(len(result_jobs["lint"].steps), 2)
        self.assertEqual(len(result_jobs["test"].steps), 3)
        self.assertEqual(len(result_jobs["build"].steps), 4)

    def test_ambiguous_step_name_across_jobs_needs_job_hint(self):
        _, _, jobs, _ = load("multi_job.yml")
        with self.assertRaises(Exception):
            resolve_selector(jobs, "Build")
        # Disambiguated with --job it resolves cleanly.
        job, idx = resolve_selector(jobs, "Build", job_hint="build")
        self.assertEqual((job, idx), ("build", 1))

    def test_unnamed_step_selected_by_index(self):
        text, lines, jobs, _ = load("unnamed_steps.yml")
        job, idx = resolve_selector(jobs, "build:1")
        result = assert_exact_splice(text, lines, jobs, job, idx, "before")
        result_jobs = injector.parse_workflow("".join(result).splitlines(keepends=True))
        self.assertEqual(len(result_jobs["build"].steps), 6)

    def test_colon_and_quoted_names_splice(self):
        text, lines, jobs, _ = load("colon_names.yml")
        job, idx = resolve_selector(jobs, "It's a test: quoted")
        assert_exact_splice(text, lines, jobs, job, idx, "before")

    def test_comments_survive_splice(self):
        text, lines, jobs, _ = load("comments.yml")
        job, idx = resolve_selector(jobs, "Run tests")
        result = assert_exact_splice(text, lines, jobs, job, idx, "before")
        result_text = "".join(result)
        self.assertIn("# Top-of-file comment.\n", result_text)
        self.assertIn("# A comment inside a step, before its run: key.\n", result_text)
        self.assertIn("# A comment after the last step (still under steps:, before dedent).\n", result_text)

    def test_crlf_line_endings_preserved(self):
        text, lines, jobs, _ = load("crlf.yml")
        job, idx = resolve_selector(jobs, "Run tests")
        result = assert_exact_splice(text, lines, jobs, job, idx, "before")
        for line in result:
            if line.strip("\r\n") == "":
                continue
            self.assertTrue(line.endswith("\r\n"), f"expected CRLF, got {line!r}")

    def test_no_trailing_newline_appends_one_before_splice(self):
        text, lines, jobs, _ = load("no_trailing_newline.yml")
        self.assertFalse(text.endswith(("\n", "\r")), "fixture must genuinely lack a trailing newline")
        job, idx = resolve_selector(jobs, "Last step")
        result = assert_exact_splice(text, lines, jobs, job, idx, "after")
        # The original last line had no newline; splicing after it must add
        # exactly one newline so the injected step isn't glued onto it.
        result_text = "".join(result)
        self.assertIn("run: echo last\n      - name:", result_text)


class InjectorGoldenTests(unittest.TestCase):
    """Hand-authored expected output for a few representative cases, typed
    independently of the production code, as a check against bugs that could
    otherwise hide inside a function both the test and the code call."""

    def test_basic_break_before_golden(self):
        text, lines, jobs, _ = load("basic.yml")
        job, idx = resolve_selector(jobs, "Run tests")
        result = injector.inject(lines, jobs, job, idx, "before")
        expected = (
            "name: CI\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "  pull_request:\n"
            "\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - name: Checkout\n"
            "        uses: actions/checkout@v4\n"
            "      - name: Install deps\n"
            "        run: pip install -r requirements.txt\n"
            '      - name: "actbreak breakpoint (before \'Run tests\' in job \'build\')"\n'
            "        shell: sh\n"
            "        run: |\n"
            "          mkdir -p /tmp/actbreak\n"
            "          : > /tmp/actbreak/hold\n"
            "          printf '%s\\n' '=================================================='\n"
            "          printf '%s\\n' 'actbreak: BREAKPOINT HIT (before)'\n"
            "          printf '%s\\n' 'actbreak:   job:  build'\n"
            "          printf '%s\\n' 'actbreak:   step: Run tests'\n"
            "          printf '%s\\n' 'actbreak: run '\"'\"'actbreak resume'\"'\"', or delete /tmp/actbreak/hold in this container'\n"
            "          printf '%s\\n' '=================================================='\n"
            "          while [ -f /tmp/actbreak/hold ]; do sleep 1; done\n"
            "          printf '%s\\n' 'actbreak: resumed, continuing workflow'\n"
            "      - name: Run tests\n"
            "        run: pytest -v\n"
            "      - run: echo done\n"
        )
        self.assertEqual("".join(result), expected)

    def test_newline_in_label_stays_on_one_block_line(self):
        # A double-quoted step name decodes "a\nb" to a real newline; it must be
        # folded so it can't break out of the run: | block and corrupt the YAML.
        lines = injector.build_hold_lines("build", "Build\nand Test", "before", 6, "\n")
        for ln in lines:
            body = ln.rstrip("\n")
            self.assertTrue(body == "" or body.startswith(" "), repr(ln))
        self.assertIn("step: Build and Test", "".join(lines))

    def test_crlf_break_after_golden(self):
        text, lines, jobs, _ = load("crlf.yml")
        job, idx = resolve_selector(jobs, "Checkout")
        result = injector.inject(lines, jobs, job, idx, "after")
        expected = (
            "name: CRLF CI\r\n"
            "on:\r\n"
            "  push:\r\n"
            "\r\n"
            "jobs:\r\n"
            "  build:\r\n"
            "    runs-on: ubuntu-latest\r\n"
            "    steps:\r\n"
            "      - name: Checkout\r\n"
            "        uses: actions/checkout@v4\r\n"
            '      - name: "actbreak breakpoint (after \'Checkout\' in job \'build\')"\r\n'
            "        shell: sh\r\n"
            "        run: |\r\n"
            "          mkdir -p /tmp/actbreak\r\n"
            "          : > /tmp/actbreak/hold\r\n"
            "          printf '%s\\n' '=================================================='\r\n"
            "          printf '%s\\n' 'actbreak: BREAKPOINT HIT (after)'\r\n"
            "          printf '%s\\n' 'actbreak:   job:  build'\r\n"
            "          printf '%s\\n' 'actbreak:   step: Checkout'\r\n"
            "          printf '%s\\n' 'actbreak: run '\"'\"'actbreak resume'\"'\"', or delete /tmp/actbreak/hold in this container'\r\n"
            "          printf '%s\\n' '=================================================='\r\n"
            "          while [ -f /tmp/actbreak/hold ]; do sleep 1; done\r\n"
            "          printf '%s\\n' 'actbreak: resumed, continuing workflow'\r\n"
            "      - name: Run tests\r\n"
            "        run: pytest -v\r\n"
        )
        self.assertEqual("".join(result), expected)


if __name__ == "__main__":
    unittest.main()
