# Contributing

Contributions are welcome under the project's AGPL-3.0-only license. Keep changes focused
on company execution controls: accounting correctness, recovery, protocol integration,
adapter usability and inspection.

## Development setup

Use Python 3.10+ on Linux or macOS:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
ruff check .
```

Run commands from the repository root. Tests use local fixtures and fictional execution
units; they do not need paid-provider credentials. For changes to installation, CLI
commands or adapters, also run the [installed-wheel onboarding audit](examples/company_onboarding/README.md#reproduce-the-installed-wheel-audit).

## Propose a change

Open an issue for a substantial change before implementing it. For a pull request,
describe the problem, resulting behavior and checks you ran. Add a regression test for
a correctness fix, especially around budgets, duplicate requests or recovery.

Preserve these invariants:

- Reserve all budget scopes before billable work begins.
- Reuse stable operation keys across retries and restarts.
- Do not refund an unknown provider outcome without evidence.
- Keep customer ownership tied to verified identity.
- Keep inspection read-only and exclude credentials and payloads from operator output.
- Keep FIFO as the supported scheduling policy and the runtime independent of LOCO-Agent.

Do not include credentials, generated signing keys, customer payloads or real execution
state in issues, commits or test fixtures. Use synthetic examples. Report vulnerabilities
through [SECURITY.md](SECURITY.md), not a public issue.

## Documentation and releases

`docs/execution-controls.md` and `poppy_controls/INTEGRATION.md` must remain identical:
the latter is bundled into the wheel and copied by `poppy-controls init`. Update
`poppy_controls/QUICKSTART.md` when generated onboarding instructions change.

Build distributions with:

```sh
python -m pip install build
python -m build
```

A successful local build does not mean a version has been published. Maintainers choose
release versions and distribution targets after the tests and installed-package audit pass.
