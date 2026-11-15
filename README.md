# Poppy Execution Controls

**Company-side execution controls for the Personal Agent Protocol.** Accept signed
requests, reserve workflow budgets, bound concurrent tool calls, and inspect charges,
reservations and denials.

This is an independent, open-source reference implementation for
[Personal Agent Protocol (Poppy)](https://personalagentprotocol.org/). It is not a
Sierra product or an official protocol implementation. Execution uses FIFO; there is
no LOCO-Agent dependency.

## Try it locally

Use Python 3.10+ on Linux or macOS. Install from source:

```sh
git clone https://github.com/ArielSmoliar/poppy-execution-controls.git
cd poppy-execution-controls
python -m venv .venv
. .venv/bin/activate
python -m pip install .
poppy-controls init --directory poppy-company
poppy-controls check --config poppy-company/controls.json
poppy-controls serve --config poppy-company/controls.json
```

In a second terminal with the same environment active:

```sh
poppy-controls demo --config poppy-company/controls.json --request-id task-1
poppy-controls inspect --config poppy-company/controls.json
```

The local airline workflow reserves **15 fictional units**, settles **12** from provider
receipts and releases **3**. Repeat `task-1`: the job and charges stay unchanged. Submit
a new request ID: the default user's remaining 3 units cannot cover another workflow,
so execution is rejected before a provider call. No airline or paid API is contacted.

`python -m poppy_controls` is equivalent to `poppy-controls`. The generated setup
contains local demo signing keys; keep those keys and execution state out of git.
These source-install instructions do not assume the package is published on PyPI.

## What it provides

- Whole-workflow budget reservations across user and company limits, with receipt-based settlement.
- FIFO execution with bounded concurrency, request-rate limits and queue backpressure.
- Durable request deduplication, bounded retries and recovery after interrupted execution.
- Conservative accounting when a provider charge is uncertain.
- A local read-only inspection CLI for receipts, holds, denials and remaining budgets.
- Replaceable company workflow and metered backend adapters.

```text
Personal agent → signed company endpoint → durable request inbox
              → workflow budget reservation → FIFO execution
              → company tools → receipt settlement and local inspection
```

This runs behind the company's API; it does not intercept arbitrary website traffic.
A workflow's billable steps must use the provided metered callback. Budget guarantees
depend on conservative quotes and provider idempotency that covers both work and billing.

## Plug in your workflow

Follow the [company integration guide](docs/execution-controls.md) for configuration,
HTTP behavior, adapter contracts, inspection and recovery.

The [order-support example](examples/company_onboarding/README.md) is an independently
installable plugin. It demonstrates a “Where is my order?” workflow and includes an
installed-wheel audit covering retries, budget rejection, inspection and crash recovery.

## Scope

Version 0.1.0 is a **single-process evaluation reference** using local SQLite state and
Unix file locks. It does not provide distributed admission, OAuth/account-linked requests,
automatic trust refresh, periodic budget resets, or full Poppy conformance. No production
throughput, cost-saving or human onboarding-time claim is made.

See [the deployment boundary](docs/execution-controls.md#http-and-deployment-boundary)
before integrating a real service. The upstream protocol remains the source of truth for
Poppy requirements.

## Contribute

Bug reports, adapter examples, documentation improvements and reproducible tests are
welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup and checks, and
[SECURITY.md](SECURITY.md) for vulnerability reports.

## License and origin

Licensed under **AGPL-3.0-only**. See [LICENSE](LICENSE).
The execution-controls code was extracted from work in
[LOCO-Agent](https://github.com/ArielSmoliar/loco-agent); attribution and provenance are
recorded in [NOTICE.md](NOTICE.md). This is now a separate project with its own package
and release cycle.
