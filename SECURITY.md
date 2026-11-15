# Security policy

This is an evaluation reference, not a hardened production deployment. Security reports
about the current development branch are welcome; there is no supported legacy-version
matrix or guaranteed response time.

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/ArielSmoliar/poppy-execution-controls/security/advisories/new).
Describe the affected version, reproduction steps, expected behavior and possible impact.
Use synthetic identities and data. Do not include live tokens, private keys or customer
records, and do not publish an exploit in a public issue while a report is being assessed.

## Deployment boundary

The reference uses one process, local SQLite state, pinned client trust and direct TLS.
The generated signing keys and loopback HTTP mode are for local development. Operators
must review their hosting, trust management, provider guarantees, credentials and data
retention before connecting real company systems.

Budget reservations depend on honest conservative quotes. The provider must supply
idempotent execution and durable receipt lookup to prevent duplicate billing and recover
unknown outcomes. Session revocation prevents future tool grants; it cannot undo a call
already dispatched.

See the [integration guide](docs/execution-controls.md#http-and-deployment-boundary) for
implemented behavior and current limitations.
