# Company execution controls

Poppy Execution Controls is an independent, open-source reference implementation for
companies adopting the [Personal Agent Protocol](https://personalagentprotocol.org/).
Accept a signed personal-agent request, reserve its workflow budget, run metered tools,
and inspect the result. The package includes a local airline demo and replaceable
backend and workflow adapters.

Execution uses FIFO. This project has no dependency on LOCO-Agent and makes no claim
of a scheduling advantage or measured production cost savings. It is an unofficial
reference for evaluation, not an implementation endorsed by Sierra or the protocol's
maintainers, and does not establish full protocol conformance.

## Install and run

Use Python 3.10+ on Linux or macOS. This implementation uses Unix file locks.
Install from a source checkout; these instructions do not assume a PyPI release:

```sh
python -m venv .venv-controls
. .venv-controls/bin/activate
python -m pip install .
poppy-controls init --directory poppy-company
poppy-controls check --config poppy-company/controls.json
poppy-controls serve --config poppy-company/controls.json
```

To distribute the reference internally, build and install a wheel instead:

```sh
python -m pip wheel --no-deps . --wheel-dir dist
# On the receiving machine, inside its virtual environment:
python -m pip install ./poppy_execution_controls-0.1.0-py3-none-any.whl
```

Record the wheel's SHA-256 when distributing it. No source checkout or editable
installation is needed to run the installed package.

In another terminal using the same virtual environment:

```sh
poppy-controls demo --config poppy-company/controls.json --request-id customer-task-1
poppy-controls inspect --config poppy-company/controls.json
poppy-controls inspect --config poppy-company/controls.json --job JOB_ID --json
```

`python -m poppy_controls` is equivalent to `poppy-controls`. `init` refuses to
overwrite a directory and generates a new private demo signing key with mode 0600.
It also writes a local README. Keep the generated key and state out of source control.

The first demo request searches route R0, checks fares and flight status, and returns
the two cheapest operating flights within the requested fare. It reserves **15 units**
for all three calls, settles **12 units** from independent local-provider receipts,
and releases **3 units**. These are fictional tariffs over real SQLite queries.
No airline or paid API is contacted.

Repeat the same command and request ID: the job and charges stay unchanged. Submit
`--request-id customer-task-2`: the default 15-unit user budget has only 3 units left,
so it reports `budget_rejected` before a backend call. HTTP 202 means durable acceptance;
poll the job to learn whether execution completed or was rejected.

## Try a custom company workflow

The [order-support onboarding exercise](../examples/company_onboarding/README.md) contains
an independently installable company package and an end-to-end audit. It replaces both
the airline workflow and backend without changing the scheduler or ledger. Follow it
for complete installation, configuration and expected-output steps.

For any local custom workflow, write its business fields to `request.json` and use:

```sh
poppy-controls submit --config company/controls.json --payload request.json --request-id task-1
poppy-controls inspect --config company/controls.json --job JOB_ID
```

`submit` uses the generated **local demo identity** and is loopback-only. It accepts
`--no-wait`, `--wait-seconds` and `--user` for local testing. Each HTTP proof is fresh;
the request ID stays fixed across retries. Exit code 0 means status retrieval succeeded;
read the JSON `state` for completed, budget-rejected or uncertain outcomes. `demo`
remains the airline shortcut. The company example and audit are source-checkout files,
not included in the project wheel; the main integration guide is copied by `init`.

## Configure the controls

`controls.json` is strict JSON; unknown settings, duplicate keys, booleans in numeric
fields, invalid limits and unsafe local HTTP bindings are rejected. Relative paths
resolve beside the config file, regardless of the shell's working directory.

| Setting | Default | Meaning |
| --- | --- | --- |
| `scheduler` | `fifo` | FIFO ordering of eligible tool calls; the only supported scheduler |
| `admission` | `workflow` | Reserve the remaining workflow bound, or use `tool` admission |
| `user_budget_units` | `15` | Cumulative limit per verified `(client_id, sub)` |
| `company_budget_units` | `120` | One cumulative limit shared across all clients and users |
| `capacity` | `4` | Maximum concurrent backend operations |
| `max_pending` | `100` | Maximum queued plus running customer jobs; at most 1,000 |
| `rate_per_minute` | `100` | New requests per trusted client in a rolling minute |
| `timeout_seconds` | `2` | Timeout per backend attempt or receipt lookup |
| `retries` | `1` | Additional attempts using the same operation key; at most 3 |
| `state_dir` | `state` | Durable inbox, journal and local demo provider database |
| `trust_file` | `trust.json` | Operator-pinned client metadata and public signing keys |
| `company` | `https://airline.example` | Canonical HTTPS audience and request-signature origin |
| `host`, `port` | `127.0.0.1`, `8765` | Listener address |
| `local_http` | `true` | Local-demo exception, restricted to loopback binding and peers |
| `backend_factory` | `poppy_controls.demo:build` | Installed `module:function` providing metered tools |
| `workflow_factory` | `poppy_controls.metered.workflow:build` | Installed `module:function` providing business steps |
| `backend_options`, `workflow_options` | `{}` | Objects passed to the respective factories |
| `contract_version` | `local-airline-v1` | Operator-managed implementation version for safe recovery |
| `version` | `1` | Configuration schema version |

Change configuration, run `check`, then restart. Existing charges and accepted
commitments survive. Budgets do not reset per session, process restart or calendar day.
Lowering a limit stops new admissions; it does not revoke previously committed work.
Backend/workflow factory names, options and `contract_version` are pinned to the state
directory. Changing them requires a new evaluation directory or a deliberate migration.
Bump `contract_version` when changing adapter semantics; never silently reinterpret old
operation IDs. A new directory has a new ledger, so it is not a production budget-reset tool.

`check` validates settings, pinned trust, factory availability and the workflow's
required methods. It reads existing state metadata to detect incompatible contract
changes before startup. It imports configured Python modules and constructs the workflow,
but does not instantiate the backend, contact a provider or create or modify execution state. It is not a connectivity or
quote-bound certification test. Treat configuration and installed plugins as trusted
operator code; keep credentials in your environment/secret store and load them in the adapter.

## Inspect and explain a request

`inspect` opens SQLite in read-only mode. It works while the service runs or after
it stops; it does not import your backend, acquire the writer lock, execute a request,
or reconcile receipts. The public HTTP API does not expose this operator view.

It shows:

- Effective configuration at last startup and visible configuration changes requiring restart.
- Company limit, settled accounting (including provisional charges), reserved budget and remaining budget.
- Verified receipt charges, provisional charges, unresolved operations and customer-job states.
- With `--job`: each operation's quote, charge and state, workflow hold history, budget denials,
  and the job owner's current budget across all their requests. This is current headroom,
  not a reconstruction of historical headroom at the moment of denial.

A completed tool inside an open workflow is already covered by the full reservation;
inspection does not add it a second time. A lost response with no recoverable receipt
retains the attempted call's quote as provisional spend and releases unused future steps.
A process interruption retains the full open commitment until startup reconciles receipts.

`inbox.sqlite` contains request identity, idempotency records and job results;
`journal.sqlite` contains operations, holds and decisions; `provider.sqlite` is the
independent demo receipt source. Operator output omits payloads, result bodies, signing
keys and factory options. The databases still contain customer data: use appropriate
filesystem permissions and retention. Each database snapshot is consistent, but the
inbox and journal are read separately, so a running request can appear slightly out of sync.
The snapshot is not a liveness check; `/healthz` reports whether the service is admitting work.

## Integrate company tools and a workflow

```text
Personal agent → signed Poppy endpoint → durable request inbox
              → workflow budget reservation → FIFO dispatch
              → company's metered backend → receipt settlement and local inspection
```

The integration sits on the company side behind its API. It does not intercept arbitrary
website traffic. Install a company-owned Python package into the service environment,
then set its factory names in `controls.json`.

A backend factory receives `build_backend(state_dir: pathlib.Path, options: dict)`.
Its object implements:

```python
from poppy_controls.metered.backend import Receipt

class CompanyBackend:
    def quote_workflow(self, task) -> int:
        # Unbilled conservative bound for ALL steps and retries that may be charged.
        ...

    def quote(self, action, params) -> int:
        # Unbilled upper bound, using tariff/shape metadata rather than executing a query.
        ...

    async def execute(self, action, params, key) -> Receipt:
        # Forward key as provider idempotency key. Return the recorded usage and charge.
        ...

    async def lookup(self, key) -> Receipt | None:
        # Recover the durable receipt without executing the business operation again.
        ...

    def close(self):
        # Synchronous cleanup, once on service shutdown.
        ...
```

`Receipt(key, data, actual_units, usage)` requires a matching operation key, list data,
nonnegative integer execution units and a JSON-serializable usage dictionary. Normalize
monetary/token usage to an agreed integer unit in the adapter. Provider idempotency
must cover billing as well as business effects. A quote is not a cap enforced by the
provider: an underestimated quote can exceed a local budget when the actual bill arrives.
If the provider cannot offer bounded charges and durable receipts, document that weaker
guarantee instead of describing the ledger as a hard monetary spending cap.

The workflow factory receives `build_workflow(options: dict)` and returns:

```python
class CompanyWorkflow:
    input_schema = {  # Published in /openapi.json; validate() enforces it separately.
        "type": "object", "additionalProperties": False,
        "properties": {"query": {"type": "string"}}, "required": ["query"],
    }

    def validate(self, task):
        # The engine supplies trusted id/owner. Validate business fields explicitly.
        if set(task) != {"id", "owner", "query"} or not isinstance(task["query"], str):
            raise ValueError("Invalid task")

    async def execute(self, task, call):
        rows = await call("lookup-v1", "lookup", {"query": task["query"]})
        return {"matches": rows}
```

Every billable step must use and await `call(step_id, action, params)`. Use stable,
unique step IDs so crash recovery reuses the same operation keys. Calls within one
workflow are serialized; different workflows share the configured capacity. Do not
spawn detached tasks or perform unmetered external work inside workflow code.
The return value is a JSON object without `state`, which the engine supplies.
`id` and `owner` in an HTTP payload are rejected; ownership comes from verified identity.

Use the [company onboarding exercise](../examples/company_onboarding/README.md)
to validate backend behavior independently. Before connecting actual workloads, test:

1. Correct results and actual receipt totals for a successful task.
2. No backend operations when whole-workflow admission rejects a task.
3. Duplicate submission and lost-response retry without extra execution or billing.
4. Stop/restart after provider commit, with the same operation IDs and no double charge.
5. Unknown receipt retains provisional accounting; inspect the unresolved request.
6. Company-wide and per-user limits under concurrent requests, without double-counting holds.

## HTTP and deployment boundary

- `GET /.well-known/poppy.json` and `GET /openapi.json`: discovery and the configured workflow schema.
- `POST /api/workflows`: signed request, `Idempotency-Key`, JSON business payload; returns 202 with job ID and status URL.
- `GET /api/workflows/{job_id}`: caller-owned status and result; other users receive 404.
- `GET /api/budget`: caller's budget only; company totals remain local.
- `POST /api/session/end`: stop that session; authorization is rechecked before each new tool grant.
- `GET /healthz`: readiness without budget or customer details.

Each HTTP retry needs a fresh Poppy proof (`jti`) but the same `Idempotency-Key` and
payload. The idempotency scope is `(client_id, sub, request_id)`, across sessions.
Changed content returns 409. Rate limiting returns 429 with `Retry-After`; a full
inbox returns 503 with `Retry-After`. Budget rejection is a terminal job state,
not a 429: waiting does not replenish these cumulative limits.

For HTTPS, replace the fictional company origin and local demo trust, set
`local_http` to `false`, configure the listener, and provide TLS:

```sh
poppy-controls serve --config company/controls.json \
  --ssl-certfile /path/to/cert.pem --ssl-keyfile /path/to/key.pem
```

The CLI disables forwarded-header trust and uses the configured origin for signature
verification. It does not automatically trust proxy headers. `create_app(config_path)`
in `poppy_controls.app` is the ASGI integration point for a separately reviewed hosting setup.
The CLI uses one worker; file locks reject concurrent writers to the same state.

This package does not yet provide multi-process/distributed admission, automatic trust
refresh/key rotation, periodic budget resets, an operator dashboard, OAuth/account-linked
requests, conversations/SSE, or complete protocol conformance certification. Session
revocation prevents new tool grants; it cannot undo an already dispatched backend call.
On restart, interrupted jobs resume only after receipt reconciliation. A terminal
`uncertain` job stays available for operator review rather than automatically repeating
unknown business effects. Long-term database retention and memory growth need operational
work before high-volume deployment. Validate on your company's workloads before making
performance or cost-saving claims.
