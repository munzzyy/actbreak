"""Resolve a user-supplied step selector against a parsed workflow.

A selector is either:

  * a step name, e.g. "Run tests" -- matched against every job's steps
    (narrowed to a single job if --job was given), or
  * "<job>:<index>", e.g. "build:2" -- a zero-based positional reference,
    for workflows with unnamed steps.
"""

from __future__ import annotations

import re

from .errors import SelectorError
from .injector import JobInfo

_JOB_INDEX_RE = re.compile(r"^([A-Za-z0-9_.-]+):(\d+)$")


def resolve_selector(
    jobs: dict[str, JobInfo], selector: str, job_hint: str | None = None
) -> tuple[str, int]:
    """Return (job_name, step_index) for `selector`, or raise SelectorError."""
    m = _JOB_INDEX_RE.match(selector)
    if m:
        job_name, idx_str = m.group(1), m.group(2)
        index = int(idx_str)
        if job_hint and job_hint != job_name:
            raise SelectorError(
                f"--job {job_hint!r} conflicts with the job named in selector {selector!r}"
            )
        if job_name not in jobs:
            raise SelectorError(
                f"job '{job_name}' not found (available jobs: {', '.join(sorted(jobs)) or 'none'})"
            )
        job = jobs[job_name]
        if index < 0 or index >= len(job.steps):
            raise SelectorError(
                f"job '{job_name}' has {len(job.steps)} step(s); index {index} is out of range"
            )
        return job_name, index

    if job_hint is not None and job_hint not in jobs:
        raise SelectorError(
            f"job '{job_hint}' not found (available jobs: {', '.join(sorted(jobs)) or 'none'})"
        )

    candidate_jobs = [job_hint] if job_hint is not None else sorted(jobs)
    matches: list[tuple[str, int]] = []
    for jname in candidate_jobs:
        for step in jobs[jname].steps:
            if step.name == selector:
                matches.append((jname, step.index))

    if not matches:
        scope = f"in job '{job_hint}'" if job_hint else "in any job"
        raise SelectorError(
            f"no step named '{selector}' found {scope}; "
            "select by position instead with '<job>:<index>'"
        )
    if len(matches) > 1:
        where = ", ".join(f"{j}:{i}" for j, i in matches)
        raise SelectorError(
            f"step name '{selector}' is ambiguous, it matches {where}; "
            "disambiguate with --job or '<job>:<index>'"
        )
    return matches[0]
