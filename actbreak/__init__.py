"""actbreak -- a local breakpoint debugger for GitHub Actions workflows.

Wraps `nektos/act` to pause a workflow run mid-job: injects a hold step
before or after a chosen step, waits for the container to reach it, then
drops you into a live shell inside the running job container.
"""

__version__ = "0.1.1"
