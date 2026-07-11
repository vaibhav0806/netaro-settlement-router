# ADR-001: Settlement Routing, Locking, and Failure Handling

- **Status:** Accepted for the assignment
- **Date:** 2026-07-12

## Context and assumptions

The service must preserve financial correctness under 1,000 concurrent
requests and an external provider with ambiguous timeouts. Correctness takes
priority over feature completeness and throughput.

Rates are positive, linear `Decimal` values. Fees, capacity, slippage, quote
expiry, and LP execution are out of scope, so the best route is independent of
amount. Only USD reservation and settlement are posted; the route and receiver
quote are audit metadata. Customer available/reserved accounts are liabilities
and Omnibus USD is an asset. `503` is assumed definitively unpaid because the
brief marks only timeouts as ambiguous. The provider deduplicates and supports
status lookup by settlement ID.

## Decision

### Locking and accounting

Use PostgreSQL `READ COMMITTED` with short, explicit pessimistic transactions.
Settlement creation is guarded by unique `(owner_id, idempotency_key)`; a loser
of a concurrent insert reloads the winner and compares the canonical request
fingerprint. Each transition locks the settlement and affected account rows
using `SELECT FOR UPDATE`, with accounts locked in ascending ID order. It
rechecks status and funds, writes one balanced journal, updates materialized
balances, and commits. Unique settlement/event journals make financial effects
idempotent.

Reservation moves Customer Available USD to Customer Reserved USD. The payout
call occurs only after a conditional `RESERVED -> PAYOUT_IN_PROGRESS`
transition commits, and no database transaction remains open during network
I/O. Success consumes the reserve against Omnibus USD; definitive failure
releases it. Timeout retains it as `PENDING_RECONCILIATION`. Reconciliation
queries the provider and atomically consumes or releases once; it never blindly
retries.

This prevents overspend and lost updates, limits deadlocks, and avoids holding
locks or connections for a five-second provider call. Recovery queries a
`PAYOUT_IN_PROGRESS` operation and submits with the same idempotency key only
if the provider definitively reports that no operation exists.

### Routing

Every 50 ms, publish an immutable rate snapshot and run max-product
Bellman-Ford from USD with deterministic tie-breaking and `(currency, LP)`
predecessors. Reject non-positive rates and snapshots with a reachable
profitable cycle. Snapshot computation is `O(VE)`; requests use `O(1)` target
lookup plus `O(V)` path reconstruction.

This deliberately differs from the brief's `O(V+E)` wording. A general cyclic,
weighted FX graph cannot maximize a multiplicative path with BFS, while
Dijkstra after `-log(rate)` is invalid when transformed weights are negative.
With five currencies, correctness is worth the negligible snapshot cost.

## Alternatives rejected

- **`SERIALIZABLE`:** correct but adds avoidable retries on hot balances;
  explicit row locks describe the actual conflict set.
- **Distributed/advisory locks:** add failure modes and cannot replace the
  PostgreSQL transaction protecting ledger state.
- **Holding a transaction across payout:** exhausts locks/connections and still
  cannot atomically commit PostgreSQL with an external provider.
- **Retrying timeouts:** risks duplicate payout; provider lookup and idempotency
  are required.
- **BFS, greedy routing, or Dijkstra:** do not correctly optimize the specified
  multiplicative graph under its unconstrained rates.

## 10,000 RPS boundary

The current design first encounters serialization on customer and Omnibus
balance rows, followed by ledger write/WAL pressure, connection limits,
synchronous provider waits, and inconsistent process-local snapshots. At that
scale, admission should return `202` into a durable queue; workers should be
partitioned by customer/account; an outbox/inbox should dispatch payouts and
results; rate snapshots should be versioned and distributed; reconciliation
should scale separately; and the journal should be partitioned. A single hot
account remains sequential unless business rules permit preallocated balance
shards reconciled to the canonical ledger.

Transport remains at-least-once with an idempotent provider effect; the system
does not claim distributed exactly-once execution.
