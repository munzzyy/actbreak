"""`actbreak init-vscode`: generate a VS Code task for every (workflow, job,
step) under .github/workflows/, each running the real `actbreak run
--break-before <job>:<index>` command for that step.

Reuses injector.parse_workflow (the same line-based scanner `run` itself
uses) rather than a YAML library, for the same reason injector.py gives for
never parsing-and-re-serializing a workflow: this module only READS the
workflow, so that reasoning doesn't strictly apply here, but reusing one
parser everywhere means a workflow shape `run` can handle and `init-vscode`
can't (or the reverse) is a bug in one file, not a second parser to keep in
sync.

tasks.json itself gets the gentler version of the same caution: it's a
user's file, and VS Code's own format allows `//` comments and trailing
commas that Python's json module rejects. We never touch an existing
tasks.json we can't confidently round-trip -- see merge_tasks_json.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import injector
from .errors import ActbreakError, VscodeTasksError

TASKS_VERSION = "2.0.0"

# Every task this module writes carries this label prefix, so a second run
# can find and replace its own prior output without touching anything a user
# added to tasks.json by hand.
LABEL_PREFIX = "actbreak: "


def discover_workflows(workflows_dir: Path) -> list[Path]:
    """*.yml/*.yaml files directly under workflows_dir, sorted for
    deterministic task ordering."""
    if not workflows_dir.is_dir():
        return []
    return sorted(
        p for p in workflows_dir.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml")
    )


def build_tasks(repo_root: Path, workflows_dir: Path) -> list[dict]:
    """One VS Code task per (workflow, job, step), sorted by workflow file
    name, then job name, then step index. A workflow actbreak's own scanner
    can't confidently parse (flow-style steps, tabs, ...) is skipped rather
    than failing the whole command -- one bad file shouldn't block tasks for
    every other workflow in the repo."""
    tasks: list[dict] = []
    for wf_path in discover_workflows(workflows_dir):
        text, _ = injector.read_workflow_text(str(wf_path))
        lines = text.splitlines(keepends=True)
        try:
            jobs = injector.parse_workflow(lines)
        except ActbreakError:
            continue

        rel_path = wf_path.relative_to(repo_root).as_posix()
        for job_name in sorted(jobs):
            for step in jobs[job_name].steps:
                selector = f"{job_name}:{step.index}"
                step_label = step.name if step.name else f"step {step.index}"
                tasks.append({
                    "label": f"{LABEL_PREFIX}{wf_path.name} / {job_name} / {step_label}",
                    "type": "shell",
                    "command": "actbreak",
                    "args": ["run", rel_path, "--break-before", selector],
                    "problemMatcher": [],
                })
    return tasks


def merge_tasks_json(existing_text: str | None, generated: list[dict]) -> tuple[str, bool]:
    """Return (new_text, safe_to_write).

    No existing file (or an empty one): build a fresh tasks.json, always
    safe to write.

    An existing file that parses as strict JSON: every prior task whose
    label starts with LABEL_PREFIX is dropped and replaced with `generated`;
    everything else the user has in there is kept byte-for-byte equivalent
    (re-serialized, so exact whitespace isn't preserved, but no task is lost
    or reordered relative to the others).

    An existing file with `//` comments or a trailing comma -- both legal
    in a real tasks.json, neither legal JSON -- can't be safely parsed and
    re-dumped without either crashing or silently eating the comments, so
    safe_to_write is False and the caller should write `generated` somewhere
    else instead of touching this file at all.
    """
    if not existing_text or not existing_text.strip():
        doc = {"version": TASKS_VERSION, "tasks": generated}
        return json.dumps(doc, indent=2) + "\n", True

    try:
        doc = json.loads(existing_text)
    except json.JSONDecodeError:
        return "", False
    if not isinstance(doc, dict):
        return "", False

    existing_tasks = doc.get("tasks")
    if not isinstance(existing_tasks, list):
        existing_tasks = []
    kept = [
        t for t in existing_tasks
        if not (isinstance(t, dict) and str(t.get("label", "")).startswith(LABEL_PREFIX))
    ]
    doc["tasks"] = kept + generated
    doc.setdefault("version", TASKS_VERSION)
    return json.dumps(doc, indent=2) + "\n", True


def write_tasks(repo_root: Path) -> tuple[Path, int, bool]:
    """Generate and write the tasks file. Returns (path written, task count,
    whether it merged into the existing .vscode/tasks.json). Raises
    VscodeTasksError if there's nothing to scan."""
    workflows_dir = repo_root / ".github" / "workflows"
    if not discover_workflows(workflows_dir):
        raise VscodeTasksError(f"no *.yml/*.yaml workflow files found in {workflows_dir}")

    tasks = build_tasks(repo_root, workflows_dir)

    vscode_dir = repo_root / ".vscode"
    tasks_path = vscode_dir / "tasks.json"
    existing_text = tasks_path.read_text(encoding="utf-8") if tasks_path.is_file() else None

    new_text, safe = merge_tasks_json(existing_text, tasks)

    vscode_dir.mkdir(parents=True, exist_ok=True)
    if safe:
        tasks_path.write_text(new_text, encoding="utf-8")
        return tasks_path, len(tasks), existing_text is not None

    fallback_path = vscode_dir / "actbreak-tasks.json"
    fallback_doc = {"version": TASKS_VERSION, "tasks": tasks}
    fallback_path.write_text(json.dumps(fallback_doc, indent=2) + "\n", encoding="utf-8")
    return fallback_path, len(tasks), False


def cmd_init_vscode(args) -> int:
    import sys

    # Lazy, matching cli.py's own reason for importing `session` lazily: keep
    # its subprocess/tempfile/signal imports off the path for --version/--help.
    from .session import find_repo_root

    repo_root = find_repo_root(Path.cwd())
    if repo_root is None:
        raise VscodeTasksError(
            "no .github/workflows directory found from the current directory upward"
        )

    path, count, merged = write_tasks(repo_root)
    if path.name == "tasks.json":
        verb = "merged into" if merged else "wrote"
        print(f"actbreak: {verb} {count} task(s) in {path}")
    else:
        print(
            f"actbreak: {repo_root / '.vscode' / 'tasks.json'} has comments or isn't plain "
            f"JSON, so it was left untouched; wrote {count} task(s) to {path} instead -- "
            "merge them in by hand",
            file=sys.stderr,
        )
    return 0
