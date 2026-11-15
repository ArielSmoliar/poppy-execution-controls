# Attribution and provenance

Poppy Execution Controls contains code extracted and adapted from the
[LOCO-Agent project](https://github.com/ArielSmoliar/loco-agent), authored by Ariel Smoliar
and contributors. The source workspace was based on LOCO-Agent commit
`3efe45b` and included subsequent, uncommitted execution-controls integration work.
That commit alone does not contain all the code extracted here.

The standalone extraction changes the package and CLI names, retains the FIFO execution
path and independent atomic ledger, and removes the LOCO scheduling dependency. The
project retains the source's **AGPL-3.0-only** license; see [LICENSE](LICENSE). Existing
copyright and license notices are preserved.

The [Personal Agent Protocol](https://personalagentprotocol.org/) is an independent
upstream standard. This repository is not affiliated with or endorsed by Sierra or
the protocol's maintainers. Protocol links describe interoperability context and do not
imply endorsement or full conformance.
