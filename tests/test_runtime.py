"""Tests for actbreak.runtime: ps-output parsing, container discovery, and
runtime/act detection -- all against fakes, never a real docker/podman/act."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from actbreak.errors import ContainerNotFoundError, ToolNotFoundError
from actbreak.runtime import (
    CommandRunner,
    Container,
    detect_runtime,
    find_job_container,
    parse_ps_output,
    require_act,
)

# Canned `docker ps --format {{.ID}}\t{{.Names}}\t{{.Status}}` / podman-equivalent
# output. Both engines produce identical shapes here since we fix the format string
# ourselves, so one fixture set covers both.
SINGLE_JOB_PS = (
    "a1b2c3d4e5f6\tact-Build-and-Test-build\tUp 2 minutes\n"
    "9988776655ff\tunrelated-container\tUp 1 hour\n"
)

MULTI_JOB_PS = (
    "111111111111\tact-BuildPipeline-lint\tUp 3 minutes\n"
    "222222222222\tact-BuildPipeline-build\tUp 2 minutes\n"
    "333333333333\tact-BuildPipeline-test\tUp 1 minute\n"
)

MATRIX_PS = (
    "aaaaaaaaaaaa\tact-Matrix-CI-test-3.10-ubuntu-latest\tUp 1 minute\n"
    "bbbbbbbbbbbb\tact-Matrix-CI-test-3.11-ubuntu-latest\tUp 1 minute\n"
)

EMPTY_PS = ""

MALFORMED_PS = "not-tab-separated-garbage\nalso garbage\n\n"


@dataclass
class FakeResult:
    stdout: str = ""
    returncode: int = 0


class FakeRunner:
    """Records every call and returns pre-programmed results, keyed by the
    joined argv string (or a default)."""

    def __init__(self, responses=None, default=None):
        self.calls = []
        self.responses = responses or {}
        self.default = default if default is not None else FakeResult()

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        key = " ".join(args)
        for pattern, result in self.responses.items():
            if pattern in key:
                return result
        return self.default


class ParsePsOutputTests(unittest.TestCase):
    def test_parses_single_job_output(self):
        containers = parse_ps_output(SINGLE_JOB_PS)
        self.assertEqual(len(containers), 2)
        self.assertEqual(containers[0], Container(id="a1b2c3d4e5f6", name="act-Build-and-Test-build", status="Up 2 minutes"))

    def test_empty_output_yields_no_containers(self):
        self.assertEqual(parse_ps_output(EMPTY_PS), [])

    def test_malformed_lines_are_skipped_not_fatal(self):
        self.assertEqual(parse_ps_output(MALFORMED_PS), [])


class FindJobContainerTests(unittest.TestCase):
    def test_single_match_found(self):
        containers = parse_ps_output(SINGLE_JOB_PS)
        c = find_job_container(containers, "build")
        self.assertEqual(c.name, "act-Build-and-Test-build")

    def test_unambiguous_job_name_resolves_without_a_hint(self):
        containers = parse_ps_output(MULTI_JOB_PS)
        c = find_job_container(containers, "test")
        self.assertEqual(c.name, "act-BuildPipeline-test")

    def test_workflow_hint_narrows_ambiguous_matches(self):
        # Two different workflows both happen to have a "build" job.
        containers = [
            Container(id="1", name="act-BuildPipeline-build"),
            Container(id="2", name="act-DeployPipeline-build"),
        ]
        with self.assertRaises(ContainerNotFoundError):
            find_job_container(containers, "build")
        c = find_job_container(containers, "build", workflow="BuildPipeline")
        self.assertEqual(c.id, "1")

    def test_matrix_job_ambiguous_without_more_context(self):
        containers = parse_ps_output(MATRIX_PS)
        with self.assertRaises(ContainerNotFoundError) as ctx:
            find_job_container(containers, "test")
        self.assertIn("multiple containers match", str(ctx.exception))

    def test_no_match_raises_with_seen_containers_listed(self):
        containers = parse_ps_output(SINGLE_JOB_PS)
        with self.assertRaises(ContainerNotFoundError) as ctx:
            find_job_container(containers, "deploy")
        self.assertIn("act-Build-and-Test-build", str(ctx.exception))

    def test_no_act_containers_at_all(self):
        with self.assertRaises(ContainerNotFoundError) as ctx:
            find_job_container([], "build")
        self.assertIn("none", str(ctx.exception))

    def test_job_name_matching_is_case_and_punctuation_insensitive(self):
        containers = [Container(id="x", name="act-My-Workflow-Say_Hello-1")]
        c = find_job_container(containers, "say hello")
        self.assertEqual(c.id, "x")

    def test_short_job_name_does_not_substring_match_a_longer_token(self):
        # job "test" must not match the "latest" token in a matrix suffix.
        containers = [Container(id="x", name="act-CI-deploy-ubuntu-latest")]
        with self.assertRaises(ContainerNotFoundError):
            find_job_container(containers, "test")

    def test_single_letter_job_does_not_match_everything(self):
        containers = [Container(id="x", name="act-Build-and-Test-build")]
        with self.assertRaises(ContainerNotFoundError):
            find_job_container(containers, "a")


class DetectRuntimeTests(unittest.TestCase):
    def test_auto_prefers_docker(self):
        which = {"docker": "/usr/bin/docker", "podman": "/usr/bin/podman"}.get
        self.assertEqual(detect_runtime("auto", which=which), "docker")

    def test_auto_falls_back_to_podman(self):
        which = {"podman": "/usr/bin/podman"}.get
        self.assertEqual(detect_runtime("auto", which=which), "podman")

    def test_auto_raises_when_neither_present(self):
        which = {}.get
        with self.assertRaises(ToolNotFoundError):
            detect_runtime("auto", which=which)

    def test_explicit_choice_respected(self):
        which = {"docker": "/usr/bin/docker", "podman": "/usr/bin/podman"}.get
        self.assertEqual(detect_runtime("podman", which=which), "podman")

    def test_explicit_choice_missing_raises(self):
        which = {"docker": "/usr/bin/docker"}.get
        with self.assertRaises(ToolNotFoundError):
            detect_runtime("podman", which=which)


class RequireActTests(unittest.TestCase):
    def test_found_returns_path(self):
        which = {"act": "/usr/local/bin/act"}.get
        self.assertEqual(require_act(which=which), "/usr/local/bin/act")

    def test_missing_raises_with_install_hint(self):
        which = {}.get
        with self.assertRaises(ToolNotFoundError) as ctx:
            require_act(which=which)
        self.assertIn("brew install act", str(ctx.exception))


class CommandRunnerTests(unittest.TestCase):
    def test_ps_builds_expected_argv_and_parses_result(self):
        fake = FakeRunner({"docker ps": FakeResult(stdout=SINGLE_JOB_PS)})
        runner = CommandRunner(run=fake)
        containers = runner.ps("docker")
        self.assertEqual(fake.calls[-1], ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Status}}"])
        self.assertEqual(len(containers), 2)

    def test_ps_all_containers_passes_dash_a(self):
        fake = FakeRunner({"docker ps": FakeResult(stdout="")})
        runner = CommandRunner(run=fake)
        runner.ps("docker", all_containers=True)
        self.assertIn("-a", fake.calls[-1])

    def test_file_exists_true_and_false(self):
        fake = FakeRunner(default=FakeResult(returncode=0))
        runner = CommandRunner(run=fake)
        self.assertTrue(runner.file_exists("docker", "c1", "/tmp/actbreak/hold"))
        self.assertEqual(fake.calls[-1], ["docker", "exec", "c1", "test", "-f", "/tmp/actbreak/hold"])

        fake2 = FakeRunner(default=FakeResult(returncode=1))
        runner2 = CommandRunner(run=fake2)
        self.assertFalse(runner2.file_exists("docker", "c1", "/tmp/actbreak/hold"))

    def test_exec_interactive_falls_back_from_sh_to_bash(self):
        fake = FakeRunner(
            {
                "exec -it c1 sh": FakeResult(returncode=127),
                "exec -it c1 bash": FakeResult(returncode=0),
            }
        )
        runner = CommandRunner(run=fake)
        rc = runner.exec_interactive("docker", "c1")
        self.assertEqual(rc, 0)
        self.assertEqual(
            fake.calls,
            [["docker", "exec", "-it", "c1", "sh"], ["docker", "exec", "-it", "c1", "bash"]],
        )

    def test_exec_interactive_does_not_fall_back_on_real_failure(self):
        fake = FakeRunner({"exec -it c1 sh": FakeResult(returncode=1)})
        runner = CommandRunner(run=fake)
        rc = runner.exec_interactive("docker", "c1")
        self.assertEqual(rc, 1)
        self.assertEqual(len(fake.calls), 1)

    def test_rm_container_passes_force_flag(self):
        fake = FakeRunner()
        runner = CommandRunner(run=fake)
        runner.rm_container("podman", "c1")
        self.assertEqual(fake.calls[-1], ["podman", "rm", "-f", "c1"])


if __name__ == "__main__":
    unittest.main()
