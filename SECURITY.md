# Security

actbreak wraps [`act`](https://github.com/nektos/act): it rewrites a copy of a
workflow to add a breakpoint step, runs it through act, and attaches a shell to
the job container with `docker exec` (or podman). Everything happens on your
machine. It opens no network listener, phones nowhere, and touches no
credentials of its own.

The things worth knowing before you run it: act executes the workflow you point
it at, so a malicious workflow can do whatever act lets workflows do inside the
container - that's act's trust model, not something actbreak adds. And the
breakpoint shell is a real root shell inside the still-running job container,
so anything you type in it happens for real. If you find a way for a workflow
file to escape the container through actbreak itself, or to make the injector
run something the workflow didn't declare, that's a vulnerability here.

## Reporting a vulnerability

Please don't open a public issue for security problems. Use GitHub's private
reporting instead:

https://github.com/munzzyy/actbreak/security/advisories/new

Include what you found, how to reproduce it, and the impact you'd expect.

## Supported versions

Fixes land on the latest tagged version; there's no backport policy.
