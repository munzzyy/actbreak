"""Shared helpers for the actbreak test suite."""

from __future__ import annotations

import hashlib
import os
import re

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture_path(name: str) -> str:
    return os.path.join(FIXTURES_DIR, name)


def act_container_name(workflow: str, job: str) -> str:
    """createContainerName("act", "<workflow>/<job>") from act's pkg/runner/run_context.go (v0.2.89)."""
    name = re.sub(r"[^a-zA-Z0-9]", "-", f"act-{workflow}/{job}").replace("--", "-")
    return f"{name[:63].strip('-')}-{hashlib.sha256(name.encode()).hexdigest()}"
