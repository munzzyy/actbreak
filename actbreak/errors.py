"""Exception types for actbreak.

Every error a user can actually hit is a subclass of ActbreakError. The CLI
catches ActbreakError at the top level and prints a clean one-line message
instead of a traceback -- tracebacks are for bugs in actbreak itself.
"""

from __future__ import annotations


class ActbreakError(Exception):
    """Base class for all expected, user-facing actbreak failures."""


class InjectionError(ActbreakError):
    """The workflow file could not be parsed or the breakpoint could not be injected."""


class SelectorError(ActbreakError):
    """The step/job selector given on the command line could not be resolved."""


class ToolNotFoundError(ActbreakError):
    """A required external tool (act, docker, podman) is missing."""


class ContainerNotFoundError(ActbreakError):
    """The job's container could not be found (or was ambiguous) via `ps`."""


class AmbiguousContainerError(ContainerNotFoundError):
    """More than one container matched the job. Distinct from the plain
    not-found case because it's a subclass of ContainerNotFoundError so
    existing `except ContainerNotFoundError` call sites still catch it
    unchanged -- callers that need to treat "ambiguous" differently from
    "not found yet, keep polling" can catch this more specific type first."""


class SessionError(ActbreakError):
    """A resume/clean operation failed against the on-disk session state."""
