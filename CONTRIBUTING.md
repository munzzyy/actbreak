# Contributing

Thanks for looking at this. It's a small, single-purpose tool and contributions are welcome.

## Setup

```
git clone https://github.com/munzzyy/actbreak
cd actbreak
pip install -e ".[dev]"
```

The tool itself has no runtime dependencies. `pytest` is the only dev dependency, and the core suite also runs under stdlib `unittest` with nothing installed.

## Running the tests

```
python -m unittest        # no dependencies needed
pytest                    # nicer output
```

The injection and selector logic is unit tested against a set of workflow fixtures in `tests/fixtures/` (quoted `on:`, matrix jobs, CRLF, unnamed steps, and the rest). Those are the correctness-critical paths, since a bad splice would corrupt someone's workflow.

The `integration` marker runs a real `act` + Docker/Podman end-to-end test. It's auto-skipped unless both are on PATH, so in practice it only runs in CI.

## What gets a change merged quickly

- A fixture for anything you fix or add. If you found a workflow shape the injector mishandles, add it to `tests/fixtures/` with the expected output. A fix with no test can silently regress.
- The injector stays line-based. Do not reach for a YAML library to "clean it up" — round-tripping corrupts real workflows (the `on:` key becomes boolean `True`, comments vanish), which is the whole reason the injector works the way it does.

## License

Contributions come in under the [Blue Oak Model License 1.0.0](https://blueoakcouncil.org/license/1.0.0). By opening a PR you agree your contribution is offered on those terms.
