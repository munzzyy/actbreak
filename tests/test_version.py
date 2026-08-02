"""actbreak's version is written down twice, in actbreak/__init__.py and in
pyproject.toml, and nothing stops them drifting. release.yml checks it before
publishing; this catches it on the way in.

pyproject.toml is read with a regex rather than tomllib because the suite has
to run on Python 3.9 with nothing installed, and tomllib landed in 3.11.
"""

from __future__ import annotations

import os
import re
import unittest

import actbreak

PYPROJECT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pyproject.toml")


def _pyproject_version() -> str:
    with open(PYPROJECT, encoding="utf-8") as fh:
        text = fh.read()
    # The first `version = "..."` after the [project] header. [build-system]
    # comes first in the file and has no version key, but anchor on [project]
    # anyway so a future section can't shadow it.
    project = text.split("[project]", 1)[1]
    match = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE)
    assert match is not None, "no version found under [project] in pyproject.toml"
    return match.group(1)


class VersionTests(unittest.TestCase):
    def test_module_version_matches_pyproject(self):
        self.assertEqual(actbreak.__version__, _pyproject_version())

    def test_version_looks_like_a_version(self):
        self.assertRegex(actbreak.__version__, r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
