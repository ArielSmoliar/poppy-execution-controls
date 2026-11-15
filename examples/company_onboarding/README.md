# Plug in a company workflow

This exercise installs an independent `company_support` Python package. Its customer
task is “Where is my order?” The workflow calls an order tool, then a delivery tool.
It contains no scheduler or budget-ledger implementation. All tools use the execution engine's `call` callback.

The provider is a local fixture with fictional prices and durable SQLite receipts.
No retailer, delivery company or paid API is contacted. Real integration effort will
depend on whether your upstream service provides bounded charges, idempotency and receipt lookup.

## Follow the developer path

From the Poppy Execution Controls source checkout, with Python 3.10+ on Linux/macOS:

```sh
python -m venv .venv-company
. .venv-company/bin/activate
python -m pip install .
python -m pip install ./examples/company_onboarding
poppy-controls init --directory order-company
```

Configure the **new, unused** setup before starting it. This script changes only the
example's factory names, version and user limit:

```sh
python - <<'PY'
import json
from pathlib import Path
path = Path('order-company/controls.json')
config = json.loads(path.read_text())
config.update(
    backend_factory='company_support.backend:build',
    workflow_factory='company_support.workflow:build',
    contract_version='order-support-v1',
    user_budget_units=8,
)
path.write_text(json.dumps(config, indent=2))
(path.parent / 'request.json').write_text('{"order_id":"ORDER-100"}\n')
PY
poppy-controls check --config order-company/controls.json
poppy-controls serve --config order-company/controls.json
```

In another terminal with the same environment active:

```sh
poppy-controls submit --config order-company/controls.json \
  --payload order-company/request.json --request-id order-1
poppy-controls inspect --config order-company/controls.json
```

Expected: `completed`, delivery status `in_transit`, **5 charged units** from two
provider receipts. The workflow reserves **8 units** up front, then releases 3.
Copy the returned `job_id` into:

```sh
poppy-controls inspect --config order-company/controls.json --job JOB_ID
```

Inspection shows operation states, workflow holds, the company budget and this
user's current remaining budget of **3 units**. It is read-only; it does not query
the provider or import your company plugin.

Repeat `submit` with `--request-id order-1`: same job, no extra provider execution
or billing. Use `--request-id order-2`: `budget_rejected`, zero additional provider
calls. Its `inspect --job JOB_ID` view shows the 8-unit quote, current 3-unit user
headroom and the denial record. Company headroom cannot override a user limit.

The submit command is a **local development client**, using generated demo keys and
loopback HTTP only. It is not a production agent SDK. `--no-wait` returns the accepted
job immediately; `--user second-customer` creates a separate local test identity.
Normal command exit means submission/status retrieval succeeded: inspect the JSON
`state` to distinguish completion, budget rejection and uncertainty. Reuse the same
request ID if polling times out. A real personal agent supplies its own signed identity.

## Replace the example with your code

- `company_support/workflow.py`: schema, field validation and business steps. Replace
  these first; the workflow receives only a metered `call(step_id, action, params)` callback.
- `company_support/backend.py`: wrap your provider SDK here. Implement quotes,
  idempotent execution, receipt lookup and cleanup. The SQLite receipt store in this
  example represents a provider guarantee; a controller-side cache alone cannot
  guarantee that a remote provider will not bill twice.
- `pyproject.toml`: give your package a company-specific name. Install it in the same
  environment as `poppy-controls`, then configure its `module:function` factories.

Use stable step IDs and bound the whole workflow conservatively. Don't put credentials
in the request body. Load them from your environment or secret store inside the adapter.
A change to factories, options or `contract_version` is incompatible with existing
state unless migrated. `check` reports that before startup. New state directories are
for separate local experiments, not a way to reset an operational budget.

## Reproduce the installed-wheel audit

Build both wheels from the source checkout:

```sh
python -m pip wheel --no-deps . ./examples/company_onboarding --wheel-dir dist
```

In a fresh virtual environment, install them (substitute absolute paths):

```sh
python -m pip install /path/to/dist/poppy_execution_controls-0.1.0-py3-none-any.whl \
  /path/to/dist/poppy_company_support_example-0.1.0-py3-none-any.whl
python -I /path/to/poppy-execution-controls/examples/company_onboarding/audit.py \
  --output /tmp/company-onboarding-results
```

The output directory must not already exist. `-I` ensures the audit imports installed
packages rather than source files beside the script. Every CLI command runs in a new
temporary directory outside the checkout. The audit starts only loopback test servers
and stops them before exiting. It writes a deliberately invalid workflow fixture into
the temporary environment to test diagnostics; do not run it in a production environment.

Assertions cover FIFO execution, custom workflow results, request deduplication,
budget denial, missing plugins, incomplete workflows, incompatible config changes,
local inspection, lost responses, unknown charges and abrupt crash recovery after a
provider commit. `report.json` contains command output, return codes, timings and receipt
checks. A failure preserves `partial.json` and server logs.

These timings measure automated command execution. They exclude installation, reading,
designing and implementing a real adapter, and are **not measured human onboarding time**.
