# Your company execution controls

The default is FIFO scheduling with whole-workflow budget reservation. Use this setup
to enforce spending and concurrency limits, deduplicate retries, recover interrupted
work, and inspect charges. Execution uses FIFO and does not require LOCO-Agent.

Run these from the directory containing this file, with your virtual environment active:

```sh
poppy-controls check --config controls.json
poppy-controls serve --config controls.json
```

In another terminal:

```sh
poppy-controls demo --config controls.json --request-id task-1
poppy-controls inspect --config controls.json
poppy-controls inspect --config controls.json --job JOB_ID --json
```

The first local task reserves 15 fictional execution units and settles 12. Repeating
the same request ID does not charge again. A new request ID is rejected before backend
work because the default user's 15-unit cumulative limit has only 3 units remaining.

For a custom workflow, put its business fields in request.json and run:

```sh
poppy-controls submit --config controls.json --payload request.json --request-id task-1
```

This uses local demo keys and is loopback-only. inspect --job JOB_ID shows both
company and current user headroom. Read the returned JSON state; exit code 0 does
not mean a budget-rejected task completed. See INTEGRATION.md for plugin contracts.

Edit controls.json to choose workflow/tool admission, budgets, concurrency,
queue limits, timeouts and installed backend/workflow factories. Run check and restart
to apply settings. Existing spending survives restart and new agent sessions.

state/ holds the durable request inbox, operation journal and independent local provider
receipts. inspect is read-only and does not load backend code. JSON output is suitable
for local automation. Do not delete state to reset a live company's budget.

agent-private.pem is a newly generated LOCAL DEMO signing key. Keep it private. Replace
the fictional origin and pinned demo trust before a real integration. The HTTP exception
is loopback-only. HTTPS mode requires a certificate and private-key file.

This is a single-process Linux/macOS reference integration, not full protocol conformance
or a validated production deployment. The company integration guide is
in INTEGRATION.md beside this file (docs/execution-controls.md in the checkout),
including plugin contracts,
HTTP semantics, recovery and deployment limitations.
