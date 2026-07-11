# Netaro Settlement Router Design

## Objective

Build a FastAPI and PostgreSQL settlement service that selects the FX route
which maximizes the receiver's target-currency output, reserves customer USD
without overspending, and records deterministic outcomes when the payout
provider succeeds, fails, or times out.

Correctness takes priority over endpoint breadth. The key invariants are:

- Each journal transaction has equal USD debits and credits.
- Available and reserved customer balances never become negative.
- One idempotency key creates at most one settlement and one reservation.
- A provider timeout never causes an immediate payout retry.
- An ambiguous payout retains its reservation until reconciliation establishes
  a definitive result.

## Components

### FX rate snapshots and routing

An in-memory rate service publishes an immutable snapshot every 50 ms. Each
snapshot contains positive directed rates from the three liquidity providers.
For parallel edges between the same currency pair, the router may retain the
LP rate that produces the greatest output because fees, capacity, slippage,
and quote expiry are outside this assignment's scope.

The router runs a max-product Bellman-Ford relaxation from USD:

```text
best[USD] = 1
best[other currencies] = 0

repeat V - 1 times:
    best[to] = max(best[to], best[from] * edge.rate)
```

Rates and calculated amounts use `Decimal`. Predecessors preserve both the
currency and LP so the route can be reconstructed. Equal-output routes use a
stable deterministic tie-break. Non-positive rates are rejected. An
additional relaxation detects profitable cycles; a snapshot with a reachable
arbitrage cycle is not used for settlement routing.

Bellman-Ford costs `O(VE)` per snapshot. This deliberately differs from the
specification's `O(V+E)` request because maximizing a product over a general
weighted cyclic graph does not have a correct linear-time solution without
additional constraints. Since rates are linear under the assignment's
assumptions, the best route is independent of the input amount. Requests read
the latest completed snapshot and perform an `O(1)` route lookup plus at most
`O(V)` path reconstruction.

### Persistence model

The database contains:

- Currency-aware accounts with account type and current balance.
- Immutable journal transactions and debit/credit postings.
- Settlements containing the request fingerprint, source amount, target
  currency, selected route, quoted output, provider operation ID, and status.
- A unique idempotency key scoped to the settlement request owner.

The schema is generic enough to seed Customer Available USD, Customer Reserved
USD, Omnibus USD, and Omnibus USDC accounts. The implemented settlement
workflow posts only in USD. The selected FX route is settlement metadata; the
assignment does not define enough LP execution or valuation behavior to invent
multi-currency accounting entries.

### Settlement state machine

Persisted states are:

```text
RESERVED -> PAYOUT_IN_PROGRESS -> SUCCESS
                               -> FAILED
                               -> PENDING_RECONCILIATION
```

`SUCCESS`, `FAILED`, and `PENDING_RECONCILIATION` are the externally visible
outcomes. `RESERVED` and `PAYOUT_IN_PROGRESS` are transient but persisted for
crash recovery.
`PENDING_RECONCILIATION` retains the reservation and may later transition to
`SUCCESS` or `FAILED` after a provider status lookup.

## Settlement flow

1. Validate the request and idempotency key. If the key already exists, return
   it for the same request fingerprint unless it is a recoverable `RESERVED`
   row; reject a different payload.
2. Read the latest valid rate snapshot and calculate the route and quoted
   output.
3. In one short database transaction:
   - Insert the settlement under a unique owner/idempotency-key constraint,
     including the quote. If concurrent inserts race, the loser rolls back,
     reloads the winner, and performs no financial work.
   - Lock affected account-balance rows with `SELECT FOR UPDATE` in stable
     account-ID order.
   - Verify sufficient customer USD.
   - Post the reservation and update balances.
   - Mark the settlement `RESERVED` and commit.
4. Lock and conditionally transition the settlement from `RESERVED` to
   `PAYOUT_IN_PROGRESS`, commit, and call the provider outside all database
   transactions and row locks. The settlement ID is the provider idempotency
   key. Only the request that wins this transition may initiate the call. A
   replay can use the same claim to recover a stranded `RESERVED` settlement.
5. In a new short transaction, process the result:
   - `200`: consume the reservation and mark `SUCCESS`.
   - `503`: release the reservation and mark `FAILED`. This design assumes the
     specification treats `503` as a definitive non-payment because it names
     only timeouts as ambiguous.
   - Timeout: preserve the reservation and mark
     `PENDING_RECONCILIATION`. Do not retry the payout.

The accounting entries are:

```text
Reservation:
Debit  Customer Available USD
Credit Customer Reserved USD

Success:
Debit  Customer Reserved USD
Credit Omnibus USD

Failure:
Debit  Customer Reserved USD
Credit Customer Available USD
```

## Reconciliation and failures

Reconciliation queries the provider using the original settlement ID; it never
blindly initiates another payout. A definitive paid result consumes the reserve
and transitions to `SUCCESS`. A definitive unpaid result releases it and
transitions to `FAILED`. An unknown result leaves both the status and reserve
unchanged.

The mock provider records a stable internal operation result. A timed-out call
may internally be paid, unpaid, or still unknown, while the initial caller sees
only the timeout. A later status lookup exposes a definitive result when one is
available. This makes timeout and reconciliation tests deterministic and avoids
random retry behavior.

A process crash after reservation leaves a recoverable persisted settlement.
Recovery of `PAYOUT_IN_PROGRESS` first looks up the provider operation. When a
provider definitively reports that no operation exists, recovery may submit
using the same provider idempotency key. It never blindly resubmits an existing
or ambiguous operation. Finalization and reconciliation lock and recheck the
settlement row before writing a unique settlement/event journal, so concurrent
workers cannot consume or release the reservation twice.

The provider operation ID is the settlement UUID and is persisted when the
settlement is created, before payout initiation.

## API surface

- `POST /settlements` creates or replays an idempotent settlement.
- `GET /settlements/{settlement_id}` returns its current status and quote.
- `POST /settlements/{settlement_id}/reconcile` queries an ambiguous provider
  operation and applies a definitive result if available.
- `GET /health` executes a lightweight PostgreSQL `SELECT 1`; it returns `200`
  with `{"status": "ok"}` when ready and `503` when the database is unavailable.

## Verification

Focused tests cover route selection, multi-hop multiplication, deterministic
ties, invalid rates, disconnected targets, arbitrage-cycle detection, and
snapshot consistency. Ledger tests cover insufficient funds, balanced entries,
reservation consumption/reversal, idempotent replay, conflicting payloads, and
all provider outcomes.

The load test sends 1,000 concurrent settlement requests with deterministic
provider outcomes. It verifies database state after completion:

- No account balance is negative.
- Each journal transaction balances.
- Each idempotency key has at most one settlement and reservation.
- Failed amounts are available again.
- Pending amounts remain reserved.
- Successful amounts are consumed exactly once and match the Omnibus USD
  movement.

## Known scaling boundary

At 10,000 requests per second, the customer-account row becomes a serialization
hotspot, PostgreSQL connections and synchronous payout waits become expensive,
and one-process rate snapshots become inconsistent across replicas. A larger
system would accept settlements into a durable queue, partition processing by
customer/account, use an outbox-backed payout worker, publish versioned rate
snapshots, and scale reconciliation independently. These changes are outside
the four-hour assignment scope.
