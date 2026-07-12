"""Tests for actbreak.selector: name-based, job:index, ambiguous, not-found."""

from __future__ import annotations

import unittest

from actbreak import injector
from actbreak.errors import SelectorError
from actbreak.selector import resolve_selector

from .util import fixture_path


def parsed(name):
    text, _ = injector.read_workflow_text(fixture_path(name))
    return injector.parse_workflow(text.splitlines(keepends=True))


class SelectorTests(unittest.TestCase):
    def test_resolve_by_name_single_job(self):
        jobs = parsed("basic.yml")
        self.assertEqual(resolve_selector(jobs, "Run tests"), ("build", 2))

    def test_resolve_by_job_index(self):
        jobs = parsed("basic.yml")
        self.assertEqual(resolve_selector(jobs, "build:3"), ("build", 3))

    def test_resolve_by_name_unique_across_multiple_jobs(self):
        jobs = parsed("multi_job.yml")
        self.assertEqual(resolve_selector(jobs, "Lint"), ("lint", 1))

    def test_ambiguous_name_across_jobs_raises(self):
        jobs = parsed("multi_job.yml")
        with self.assertRaises(SelectorError) as ctx:
            resolve_selector(jobs, "Checkout")
        msg = str(ctx.exception).lower()
        self.assertIn("ambiguous", msg)

    def test_ambiguous_name_resolved_with_job_hint(self):
        jobs = parsed("multi_job.yml")
        self.assertEqual(resolve_selector(jobs, "Checkout", job_hint="build"), ("build", 0))

    def test_name_not_found_raises(self):
        jobs = parsed("basic.yml")
        with self.assertRaises(SelectorError) as ctx:
            resolve_selector(jobs, "Nonexistent step")
        self.assertIn("no step named", str(ctx.exception))

    def test_job_index_out_of_range_raises(self):
        jobs = parsed("basic.yml")
        with self.assertRaises(SelectorError) as ctx:
            resolve_selector(jobs, "build:99")
        self.assertIn("out of range", str(ctx.exception))

    def test_job_index_unknown_job_raises(self):
        jobs = parsed("basic.yml")
        with self.assertRaises(SelectorError) as ctx:
            resolve_selector(jobs, "nope:0")
        self.assertIn("not found", str(ctx.exception))

    def test_job_hint_conflicts_with_selector_job_raises(self):
        jobs = parsed("multi_job.yml")
        with self.assertRaises(SelectorError):
            resolve_selector(jobs, "build:0", job_hint="lint")

    def test_job_hint_unknown_job_raises_even_for_name_search(self):
        jobs = parsed("basic.yml")
        with self.assertRaises(SelectorError) as ctx:
            resolve_selector(jobs, "Run tests", job_hint="nope")
        self.assertIn("not found", str(ctx.exception))

    def test_unnamed_step_only_reachable_by_index(self):
        jobs = parsed("unnamed_steps.yml")
        # There is no name to match for index 0/1/3, only positional selection works.
        self.assertEqual(resolve_selector(jobs, "build:0"), ("build", 0))
        with self.assertRaises(SelectorError):
            resolve_selector(jobs, "")

    def test_step_named_like_a_job_index_falls_back_to_name_match(self):
        # A step literally named "deploy:2" matches the job:index regex, but
        # there's no job called "deploy" -- must retry as a literal name
        # instead of raising "job 'deploy' not found".
        jobs = parsed("colon_step_name.yml")
        self.assertEqual(resolve_selector(jobs, "deploy:2"), ("build", 1))

    def test_job_index_still_wins_when_it_actually_resolves(self):
        # Regression guard: the name-fallback must not shadow a genuinely
        # valid job:index selector.
        jobs = parsed("colon_step_name.yml")
        self.assertEqual(resolve_selector(jobs, "build:1"), ("build", 1))


if __name__ == "__main__":
    unittest.main()
