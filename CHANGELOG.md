# Changelog

## 0.1.0 — Initial standalone version

- Accept signed personal-agent requests through a company-side reference API.
- Reserve user and company workflow budgets before executing metered tools; settle
  charges from receipts and retain conservative accounting for unknown outcomes.
- Use FIFO with bounded concurrency, rate limiting and queue backpressure.
- Deduplicate requests and recover interrupted work using stable operation keys.
- Initialize and validate configuration, submit local test requests, and inspect durable
  receipts, holds and denials with `poppy-controls`.
- Plug in independent workflow and backend adapters; exercise them with the order-support
  onboarding example and installed-wheel audit.
- Extract execution controls from the LOCO-Agent workspace into an independent package,
  retaining AGPL-3.0-only licensing and attribution, without a LOCO-Agent dependency.

This entry describes the initial source version. It does not claim publication on PyPI.
