"""Shared helpers for the actbreak test suite."""

from __future__ import annotations

import os

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture_path(name: str) -> str:
    return os.path.join(FIXTURES_DIR, name)
