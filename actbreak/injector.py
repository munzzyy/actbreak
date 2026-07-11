"""Line-based breakpoint injection into GitHub Actions workflow files.

This is deliberately NOT a YAML parser. We never parse-and-re-serialize the
workflow with a YAML library: PyYAML's default loader coerces an unquoted
`on:` key to the boolean `True` under YAML 1.1 rules, and any round trip
through a generic dumper destroys comments, quoting style, and anchors. A
user's workflow file is precious -- we touch only the lines we must.

Instead we do indentation-based structural scanning, the same way a human
skims a workflow file: find `jobs:`, find the job block, find its `steps:`
list, walk the `- ` items at that indentation, and splice a new list item in
at the right place. Everything else is copied through byte-for-byte.

Supported step body shapes (all common in real workflows):

    - name: Build
      run: make build

    - run: make build          # unnamed step, matched by index only

    -
      name: Build
      run: make build

Not supported (raises InjectionError with a clear message):

    - flow-style `steps: [a, b]`
    - tab characters anywhere in a line's leading indentation
    - a workflow with no top-level `jobs:` key
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .errors import InjectionError

_KEY_LINE_RE = re.compile(r"^( *)([^\s:][^:]*):(?:\s.*)?$")
_DASH_LINE_RE = re.compile(r"^( *)-(?:\s(.*)|)$")
_JOBS_LINE_RE = re.compile(r"^jobs:(?:\s*#.*)?$")
_NAME_KEY_RE = re.compile(r"^name:\s*(.*)$")


@dataclass
class StepInfo:
    """One step in a job's `steps:` list."""

    job: str
    index: int
    name: str | None
    start: int  # index into the file's line list of the `- ` marker line
    end: int  # exclusive; first line index after this step's block


@dataclass
class JobInfo:
    """One job under `jobs:`."""

    name: str
    start: int
    end: int
    step_item_indent: int | None  # column of `- ` for this job's steps, if any
    steps: list[StepInfo] = field(default_factory=list)


def _rstrip_eol(line: str) -> str:
    return line.rstrip("\r\n")


def _indent(line: str) -> int:
    stripped = _rstrip_eol(line)
    return len(stripped) - len(stripped.lstrip(" "))


def _is_blank_or_comment(line: str) -> bool:
    content = _rstrip_eol(line).strip()
    return content == "" or content.startswith("#")


def _check_no_tabs(lines: list[str]) -> None:
    for i, line in enumerate(lines):
        content = _rstrip_eol(line)
        j = 0
        while j < len(content) and content[j] in " \t":
            j += 1
        if "\t" in content[:j]:
            raise InjectionError(
                f"line {i + 1}: tab characters in indentation are not supported "
                "by actbreak's line-based injector; re-indent this workflow with spaces"
            )


def _first_content(lines: list[str], start: int, limit: int) -> tuple[int | None, int | None]:
    """Return (index, indent) of the first non-blank/non-comment line in [start, limit)."""
    for i in range(start, limit):
        if not _is_blank_or_comment(lines[i]):
            return i, _indent(lines[i])
    return None, None


def _next_boundary(lines: list[str], start: int, limit: int, max_indent: int) -> int:
    """First index >= start, < limit whose indentation is <= max_indent (a dedent
    or a sibling key/item). Blank/comment lines never count. Returns `limit` if
    no such line exists."""
    for i in range(start, limit):
        if _is_blank_or_comment(lines[i]):
            continue
        if _indent(lines[i]) <= max_indent:
            return i
    return limit


def _dequote(key: str) -> str:
    key = key.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        return key[1:-1]
    return key


def _parse_scalar_value(raw: str) -> str:
    """Best-effort YAML scalar decode for a `name:` value, good enough to match
    against a user-supplied selector string. Handles double-quoted, single-quoted,
    and plain scalars (stripping a trailing ` # comment`)."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] == '"':
        # Double-quoted scalar: find the closing quote, honoring backslash escapes.
        out = []
        i = 1
        while i < len(raw):
            c = raw[i]
            if c == "\\" and i + 1 < len(raw):
                nxt = raw[i + 1]
                unescape = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
                out.append(unescape.get(nxt, nxt))
                i += 2
                continue
            if c == '"':
                break
            out.append(c)
            i += 1
        return "".join(out)
    if raw[0] == "'":
        # Single-quoted scalar: '' is an escaped literal quote.
        out = []
        i = 1
        while i < len(raw):
            if raw[i] == "'":
                if i + 1 < len(raw) and raw[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(raw[i])
            i += 1
        return "".join(out)
    # Plain scalar: a ` #` (whitespace before hash) starts a trailing comment.
    m = re.search(r"\s#", raw)
    if m:
        raw = raw[: m.start()]
    return raw.strip()


def _step_body_columns(lines: list[str], start: int, end: int, dash_indent: int) -> list[tuple[int, str]]:
    """Yield (column, text-after-column) for every logical key line inside a
    step block, treating `- key: value` (key inline with the dash) as a key
    line at the column right after `- `."""
    items: list[tuple[int, str]] = []
    raw0 = _rstrip_eol(lines[start])
    after_dash = raw0[dash_indent + 1 :]
    lstripped = after_dash.lstrip(" ")
    col0 = dash_indent + 1 + (len(after_dash) - len(lstripped))
    if lstripped != "":
        items.append((col0, lstripped))
    for i in range(start + 1, end):
        if _is_blank_or_comment(lines[i]):
            continue
        text = _rstrip_eol(lines[i])
        col = _indent(text)
        items.append((col, text[col:]))
    return items


def _extract_step_name(lines: list[str], start: int, end: int, dash_indent: int) -> str | None:
    items = _step_body_columns(lines, start, end, dash_indent)
    if not items:
        return None
    body_col = items[0][0]
    for col, text in items:
        if col != body_col:
            continue
        m = _NAME_KEY_RE.match(text)
        if m:
            return _parse_scalar_value(m.group(1))
    return None


def _parse_job_steps(lines: list[str], job: JobInfo) -> None:
    body_start, body_indent = _first_content(lines, job.start + 1, job.end)
    if body_start is None:
        return  # empty job body (unusual, but not our problem to fix)

    steps_line = None
    i = body_start
    while i < job.end:
        if _is_blank_or_comment(lines[i]):
            i += 1
            continue
        indent = _indent(lines[i])
        if indent < body_indent:
            break
        if indent == body_indent:
            m = _KEY_LINE_RE.match(_rstrip_eol(lines[i]))
            if m and _dequote(m.group(2)) == "steps":
                steps_line = i
                break
            i = _next_boundary(lines, i + 1, job.end, body_indent)
            continue
        i += 1

    if steps_line is None:
        return  # job has no steps: (e.g. it only calls a reusable workflow via `uses:`)

    step_start0, step_item_indent = _first_content(lines, steps_line + 1, job.end)
    if step_start0 is None or step_item_indent is None or step_item_indent <= body_indent:
        raise InjectionError(f"job '{job.name}': 'steps:' has no recognizable list of steps")
    first_line = _rstrip_eol(lines[step_start0])
    if not (len(first_line) > step_item_indent and first_line[step_item_indent] == "-"):
        raise InjectionError(
            f"job '{job.name}': 'steps:' must be a block list (`- ...`); "
            "flow-style sequences are not supported"
        )

    job.step_item_indent = step_item_indent
    idx = 0
    i = step_start0
    while i < job.end:
        if _is_blank_or_comment(lines[i]):
            i += 1
            continue
        indent = _indent(lines[i])
        if indent < step_item_indent:
            break
        if indent > step_item_indent:
            i += 1
            continue
        m = _DASH_LINE_RE.match(_rstrip_eol(lines[i]))
        if not m:
            raise InjectionError(f"job '{job.name}': malformed step list item at line {i + 1}")
        step_end = _next_boundary(lines, i + 1, job.end, step_item_indent)
        name = _extract_step_name(lines, i, step_end, step_item_indent)
        job.steps.append(StepInfo(job=job.name, index=idx, name=name, start=i, end=step_end))
        idx += 1
        i = step_end


def parse_workflow(lines: list[str]) -> dict[str, JobInfo]:
    """Parse the structural skeleton of a workflow file (job blocks and their
    steps) without ever building a full YAML document. Raises InjectionError
    on anything we can't confidently scan."""
    _check_no_tabs(lines)

    jobs_line = None
    for i, line in enumerate(lines):
        content = _rstrip_eol(line)
        if content.startswith(" ") or content.startswith("\t"):
            continue
        if _JOBS_LINE_RE.match(content):
            jobs_line = i
            break
    if jobs_line is None:
        raise InjectionError("no top-level 'jobs:' key found in this workflow file")

    job_start, job_indent = _first_content(lines, jobs_line + 1, len(lines))
    if job_start is None or job_indent is None or job_indent == 0:
        raise InjectionError("'jobs:' has no jobs defined under it")

    jobs: dict[str, JobInfo] = {}
    i = job_start
    while i < len(lines):
        if _is_blank_or_comment(lines[i]):
            i += 1
            continue
        indent = _indent(lines[i])
        if indent < job_indent:
            break
        if indent > job_indent:
            i += 1
            continue
        m = _KEY_LINE_RE.match(_rstrip_eol(lines[i]))
        if not m:
            raise InjectionError(f"could not parse job key at line {i + 1}")
        name = _dequote(m.group(2))
        end = _next_boundary(lines, i + 1, len(lines), job_indent)
        job = JobInfo(name=name, start=i, end=end, step_item_indent=None)
        _parse_job_steps(lines, job)
        jobs[name] = job
        i = end

    if not jobs:
        raise InjectionError("'jobs:' has no jobs defined under it")
    return jobs


def _detect_newline(text: str) -> str:
    idx = text.find("\n")
    if idx > 0 and text[idx - 1] == "\r":
        return "\r\n"
    return "\n"


def _sh_single_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def build_hold_lines(job: str, label: str, position: str, dash_indent: int, newline: str) -> list[str]:
    """Build the synthetic breakpoint step, as a list of complete lines (each
    already carrying `newline`), indented to slot into `job`'s steps list at
    `dash_indent`."""
    key_indent = dash_indent + 2
    body_indent = key_indent + 2
    step_name = f"actbreak breakpoint ({position} '{label}' in job '{job}')"

    out: list[str] = []

    def emit(text: str, indent: int) -> None:
        out.append(" " * indent + text + newline)

    emit(f"- name: {json.dumps(step_name)}", dash_indent)
    emit("shell: sh", key_indent)
    emit("run: |", key_indent)

    script = [
        "mkdir -p /tmp/actbreak",
        ": > /tmp/actbreak/hold",
        "echo " + _sh_single_quote("=================================================="),
        "echo " + _sh_single_quote(f"actbreak: BREAKPOINT HIT ({position})"),
        "echo " + _sh_single_quote(f"actbreak:   job:  {job}"),
        "echo " + _sh_single_quote(f"actbreak:   step: {label}"),
        "echo " + _sh_single_quote(
            "actbreak: run 'actbreak resume', or delete /tmp/actbreak/hold in this container"
        ),
        "echo " + _sh_single_quote("=================================================="),
        "while [ -f /tmp/actbreak/hold ]; do sleep 1; done",
        "echo " + _sh_single_quote("actbreak: resumed, continuing workflow"),
    ]
    for line in script:
        emit(line, body_indent)
    return out


def inject(lines: list[str], jobs: dict[str, JobInfo], job: str, step_index: int, position: str) -> list[str]:
    """Return a new list of lines with the hold step spliced in. `position` is
    'before' or 'after' the target step. Does not mutate `lines`."""
    if position not in ("before", "after"):
        raise ValueError(f"position must be 'before' or 'after', got {position!r}")
    if job not in jobs:
        raise InjectionError(f"job '{job}' not found")
    j = jobs[job]
    if step_index < 0 or step_index >= len(j.steps):
        raise InjectionError(f"job '{job}' has {len(j.steps)} steps; index {step_index} out of range")
    if j.step_item_indent is None:
        raise InjectionError(f"job '{job}' has no steps to inject relative to")

    step = j.steps[step_index]
    insert_at = step.start if position == "before" else step.end
    label = step.name if step.name is not None else f"{job}:{step_index}"

    newline = _detect_newline("".join(lines)) if lines else "\n"
    new_lines = list(lines)

    if insert_at == len(new_lines) and new_lines and not new_lines[-1].endswith(("\n", "\r")):
        new_lines[-1] = new_lines[-1] + newline

    hold_lines = build_hold_lines(job, label, position, j.step_item_indent, newline)
    return new_lines[:insert_at] + hold_lines + new_lines[insert_at:]


def read_workflow_text(path: str) -> tuple[str, bool]:
    """Read a workflow file, returning (decoded text, had_utf8_bom)."""
    with open(path, "rb") as f:
        raw = f.read()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if has_bom else raw
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise InjectionError(f"{path}: not valid UTF-8 text ({e})") from e
    return text, has_bom


def write_workflow_text(path: str, text: str, has_bom: bool) -> None:
    prefix = "\ufeff" if has_bom else ""
    data = (prefix + text).encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)


def extract_workflow_name(lines: list[str]) -> str | None:
    """Best-effort read of the top-level `name:` field (the display name act
    and the GitHub UI use), for narrowing container discovery. Returns None
    if there isn't one (act then falls back to the workflow's file path)."""
    for line in lines:
        content = _rstrip_eol(line)
        if content.startswith(" ") or content.startswith("\t"):
            continue
        if content.startswith("jobs:"):
            break
        m = re.match(r"^name:\s*(.*)$", content)
        if m:
            return _parse_scalar_value(m.group(1))
    return None


def inject_file(src_path: str, dest_path: str, job: str, step_index: int, position: str) -> str:
    """Parse `src_path`, inject a hold step, write the result to `dest_path`.
    Returns the resolved step label (name, or 'job:index' if unnamed)."""
    text, has_bom = read_workflow_text(src_path)
    lines = text.splitlines(keepends=True)
    jobs = parse_workflow(lines)
    if job not in jobs:
        raise InjectionError(f"job '{job}' not found in {src_path}")
    step = jobs[job].steps[step_index] if 0 <= step_index < len(jobs[job].steps) else None
    label = step.name if step and step.name else f"{job}:{step_index}"
    new_lines = inject(lines, jobs, job, step_index, position)
    write_workflow_text(dest_path, "".join(new_lines), has_bom)
    return label
