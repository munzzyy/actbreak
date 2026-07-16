"""Tests for actbreak.vscode_tasks: task generation from real workflow
fixtures, the tasks.json merge/fallback logic, and cmd_init_vscode's
repo-root and error handling."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actbreak import session, vscode_tasks
from actbreak.errors import VscodeTasksError

from .util import fixture_path


def copy_fixture(fixture_name: str, dest_dir: Path, dest_name: str | None = None) -> Path:
    dest = dest_dir / (dest_name or fixture_name)
    dest.write_text(Path(fixture_path(fixture_name)).read_text(encoding="utf-8"), encoding="utf-8")
    return dest


class DiscoverWorkflowsTests(unittest.TestCase):
    def test_finds_yml_and_yaml_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            wf_dir = Path(td)
            (wf_dir / "b.yaml").write_text("jobs:\n  x:\n    steps:\n      - run: echo\n")
            (wf_dir / "a.yml").write_text("jobs:\n  x:\n    steps:\n      - run: echo\n")
            (wf_dir / "readme.md").write_text("not a workflow")
            found = vscode_tasks.discover_workflows(wf_dir)
            self.assertEqual([p.name for p in found], ["a.yml", "b.yaml"])

    def test_missing_directory_returns_empty(self):
        self.assertEqual(vscode_tasks.discover_workflows(Path("/no/such/dir")), [])


class BuildTasksTests(unittest.TestCase):
    def test_multi_job_workflow_gets_one_task_per_step(self):
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            wf_dir = repo_root / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            copy_fixture("multi_job.yml", wf_dir)

            tasks = vscode_tasks.build_tasks(repo_root, wf_dir)

            # lint (2 steps) + build (3 steps) + test (3 steps) = 8
            self.assertEqual(len(tasks), 8)
            labels = [t["label"] for t in tasks]
            self.assertIn("actbreak: multi_job.yml / build / Checkout", labels)
            self.assertIn("actbreak: multi_job.yml / build / Upload artifact", labels)
            self.assertIn("actbreak: multi_job.yml / lint / Lint", labels)
            # jobs sorted alphabetically: build, lint, test
            job_order = [lbl.split(" / ")[1] for lbl in labels]
            self.assertEqual(job_order, sorted(job_order))

    def test_task_args_run_the_real_break_before_command(self):
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            wf_dir = repo_root / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            copy_fixture("multi_job.yml", wf_dir)

            tasks = vscode_tasks.build_tasks(repo_root, wf_dir)
            build_checkout = next(t for t in tasks if t["label"] == "actbreak: multi_job.yml / build / Checkout")
            self.assertEqual(build_checkout["type"], "shell")
            self.assertEqual(build_checkout["command"], "actbreak")
            self.assertEqual(
                build_checkout["args"],
                ["run", ".github/workflows/multi_job.yml", "--break-before", "build:0"],
            )

    def test_unnamed_steps_fall_back_to_step_index_label(self):
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            wf_dir = repo_root / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            copy_fixture("unnamed_steps.yml", wf_dir)

            tasks = vscode_tasks.build_tasks(repo_root, wf_dir)
            labels = [t["label"] for t in tasks]
            self.assertIn("actbreak: unnamed_steps.yml / build / step 0", labels)
            self.assertIn("actbreak: unnamed_steps.yml / build / Run tests", labels)

    def test_a_workflow_the_scanner_cannot_parse_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            wf_dir = repo_root / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            copy_fixture("tabs.yml", wf_dir)  # raises InjectionError when parsed
            copy_fixture("multi_job.yml", wf_dir)

            tasks = vscode_tasks.build_tasks(repo_root, wf_dir)
            # Only multi_job.yml's 8 steps show up; tabs.yml contributes nothing.
            self.assertEqual(len(tasks), 8)
            self.assertTrue(all("tabs.yml" not in t["label"] for t in tasks))


class MergeTasksJsonTests(unittest.TestCase):
    def test_no_existing_file_builds_fresh_document(self):
        generated = [{"label": "actbreak: a", "type": "shell", "command": "actbreak", "args": []}]
        text, safe = vscode_tasks.merge_tasks_json(None, generated)
        self.assertTrue(safe)
        doc = json.loads(text)
        self.assertEqual(doc["version"], vscode_tasks.TASKS_VERSION)
        self.assertEqual(doc["tasks"], generated)

    def test_existing_valid_json_keeps_user_tasks(self):
        existing = json.dumps({
            "version": "2.0.0",
            "tasks": [{"label": "My Own Task", "type": "shell", "command": "echo hi"}],
        })
        generated = [{"label": "actbreak: a", "type": "shell", "command": "actbreak", "args": []}]
        text, safe = vscode_tasks.merge_tasks_json(existing, generated)
        self.assertTrue(safe)
        labels = [t["label"] for t in json.loads(text)["tasks"]]
        self.assertIn("My Own Task", labels)
        self.assertIn("actbreak: a", labels)

    def test_rerun_replaces_prior_actbreak_tasks_without_duplicating(self):
        existing = json.dumps({
            "version": "2.0.0",
            "tasks": [
                {"label": "My Own Task", "type": "shell", "command": "echo hi"},
                {"label": "actbreak: stale", "type": "shell", "command": "actbreak", "args": []},
            ],
        })
        generated = [{"label": "actbreak: fresh", "type": "shell", "command": "actbreak", "args": []}]
        text, safe = vscode_tasks.merge_tasks_json(existing, generated)
        self.assertTrue(safe)
        labels = [t["label"] for t in json.loads(text)["tasks"]]
        self.assertEqual(labels, ["My Own Task", "actbreak: fresh"])

    def test_jsonc_comments_are_not_safe_to_merge(self):
        existing = '{\n  // a comment\n  "tasks": [],\n}\n'
        text, safe = vscode_tasks.merge_tasks_json(existing, [])
        self.assertFalse(safe)
        self.assertEqual(text, "")

    def test_non_object_json_is_not_safe_to_merge(self):
        text, safe = vscode_tasks.merge_tasks_json("[1, 2, 3]", [])
        self.assertFalse(safe)


class WriteTasksTests(unittest.TestCase):
    def _make_repo(self, tmp_path: Path) -> Path:
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        copy_fixture("multi_job.yml", wf_dir, "ci.yml")
        return wf_dir

    def test_writes_fresh_tasks_json(self):
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            self._make_repo(repo_root)

            path, count, merged = vscode_tasks.write_tasks(repo_root)

            self.assertEqual(path, repo_root / ".vscode" / "tasks.json")
            self.assertEqual(count, 8)
            self.assertFalse(merged)
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(doc["tasks"]), 8)

    def test_rerunning_merges_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            self._make_repo(repo_root)

            vscode_tasks.write_tasks(repo_root)
            path, count, merged = vscode_tasks.write_tasks(repo_root)

            self.assertTrue(merged)
            self.assertEqual(count, 8)
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(doc["tasks"]), 8)  # no duplicates

    def test_jsonc_tasks_json_falls_back_to_a_separate_file(self):
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            self._make_repo(repo_root)
            vscode_dir = repo_root / ".vscode"
            vscode_dir.mkdir()
            original = '{\n  // hand-written\n  "tasks": [],\n}\n'
            (vscode_dir / "tasks.json").write_text(original, encoding="utf-8")

            path, count, merged = vscode_tasks.write_tasks(repo_root)

            self.assertEqual(path, vscode_dir / "actbreak-tasks.json")
            self.assertFalse(merged)
            self.assertEqual(count, 8)
            # The original, comments included, is untouched.
            self.assertEqual((vscode_dir / "tasks.json").read_text(encoding="utf-8"), original)
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(doc["tasks"]), 8)

    def test_no_workflow_files_raises(self):
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            (repo_root / ".github" / "workflows").mkdir(parents=True)
            with self.assertRaises(VscodeTasksError):
                vscode_tasks.write_tasks(repo_root)


class CmdInitVscodeTests(unittest.TestCase):
    def test_no_repo_root_raises_vscode_tasks_error(self):
        with mock.patch.object(session, "find_repo_root", return_value=None):
            with self.assertRaises(VscodeTasksError) as ctx:
                vscode_tasks.cmd_init_vscode(mock.Mock())
        self.assertIn("no .github/workflows directory", str(ctx.exception))

    def test_success_path_writes_tasks_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            wf_dir = repo_root / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            copy_fixture("multi_job.yml", wf_dir, "ci.yml")

            with mock.patch.object(session, "find_repo_root", return_value=repo_root):
                code = vscode_tasks.cmd_init_vscode(mock.Mock())

            self.assertEqual(code, 0)
            self.assertTrue((repo_root / ".vscode" / "tasks.json").is_file())


if __name__ == "__main__":
    unittest.main()
