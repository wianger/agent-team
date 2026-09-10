# The event log is authoritative; backend sessions are disposable caches

Every member's provider-side backend session could plausibly hold the canonical conversation
state — the providers resume them, and trusting them would remove a whole subsystem. We keep an
append-only SQLite event log per room instead, and treat backend sessions as caches that may be
lost, rebuilt, or diverge at any time.

The reason is that we do not control backend session lifetime, retention, or semantics, and they
differ per provider: Codex sessions live behind a resident app-server, Claude's behind a streaming
CLI. A room outlives any of them. Anchoring on the log means a lost or incompatible session costs
a rebuild rather than a corrupted history, and it makes recovery expressible as a cursor into the
log rather than as provider-specific state surgery.

## Consequences

Every recovery path must reconcile against the log: a resumed session is replayed the messages it
has not acknowledged, with a reconciliation notice. Divergence between a session and the log is
always resolved in the log's favour, and a session may be discarded whenever that is cheaper than
reconciling it.
