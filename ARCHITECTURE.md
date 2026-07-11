# Netaro Router Architecture

## Purpose and scope

Netaro Router is a FastAPI and PostgreSQL settlement service. It chooses the FX
route that maximizes the receiver's target-currency output, reserves customer
USD without overspending, invokes an unreliable payout provider, and persists
a deterministic outcome.

The assignment implementation excludes LP fees, liquidity capacity, slippage,
quote expiry, real LP trade execution, and multi-currency valuation postings.
It seeds the required Omnibus USD and USDC accounts, but the implemented ledger
flow posts in USD only.

The system preserves these invariants:

- Every USD journal has equal debits and credits.
- Customer available and reserved balances never become negative.
- An owner-scoped idempotency key creates at most one settlement and reserve.
- Database locks are never held across provider I/O.
- A timeout never causes an immediate payout retry.
- An ambiguous payout retains exactly one reservation.

## Components

```text
Rate simulator (50 ms) -> immutable snapshot -> max-product router
                                                  |
Client -> FastAPI -> settlement orchestrator ------+
                       |              |
                       v              v
                  ledger service   payout provider
                       |              |
                       v              v
                   PostgreSQL    operation lookup
                       ^              |
                       +-- reconciliation service
```

- **FastAPI layer:** validates requests, extracts idempotency keys, and exposes
  settlement, reconciliation, status, and health endpoints.
- **Settlement orchestrator:** owns legal state transitions and transaction
  boundaries.
- **Rate service:** atomically publishes a completed, versioned, immutable graph
  snapshot every 50 ms.
- **Router:** precomputes best USD routes for the current snapshot.
- **Ledger service:** writes immutable journals and updates materialized account
  balances in the same transaction.
- **Payout provider:** returns success, definitive failure, or an ambiguous
  timeout and deduplicates operations by settlement ID.
- **Reconciliation service:** queries an existing provider operation and applies
  a definitive result without blindly retrying it.

## FX routing

The graph contains USD, USDC, EUR, PHP, and AED with directed `Decimal` rates
from three LPs. For parallel edges, the greatest-output LP is sufficient under
the assignment's no-fee/no-capacity assumptions.

For each completed snapshot, the router runs max-product Bellman-Ford from USD:

```text
best[USD] = 1
best[others] = 0

repeat V - 1 times:
    best[to] = max(best[to], best[from] * rate)
```

The predecessor stores currency and LP. Ties use a stable ordering. Non-positive
rates are invalid, disconnected targets have no route, and a reachable
profitable cycle invalidates the snapshot. Bellman-Ford costs `O(VE)` during
snapshot publication; a request performs `O(1)` target lookup and at most
`O(V)` path reconstruction. Each settlement stores the snapshot version, path,
aggregate rate, and quoted receiver amount.

## Persistence model

### Accounts

`accounts` contains an owner, currency, account class (`ASSET` or `LIABILITY`),
purpose, and materialized balance. Required seeded accounts are Customer
Available USD, Customer Reserved USD, Omnibus USD, and Omnibus USDC. Asset and
liability balances follow their normal debit/credit sides. Seed balances are
created through an opening journal rather than direct balance mutation.

### Ledger

`journal_transactions` identifies the settlement and event such as `RESERVE`,
`CONSUME`, or `RELEASE`. A unique settlement/event constraint prevents duplicate
financial effects. Immutable `postings` contain an account, debit/credit side,
positive amount, and currency. Account balances and postings change atomically.

```text
Reservation: Debit Customer Available USD / Credit Customer Reserved USD
Success:     Debit Customer Reserved USD  / Credit Omnibus USD
Failure:     Debit Customer Reserved USD  / Credit Customer Available USD
```

### Settlements

`settlements` stores owner, idempotency key, canonical request fingerprint,
source amount, target currency, route and snapshot data, provider operation ID,
status, and timestamps. `(owner_id, idempotency_key)` is unique. Reusing a key
with the same fingerprint returns the existing settlement; a different
fingerprint is a conflict.

## State machine

```text
RESERVED -> PAYOUT_IN_PROGRESS -> SUCCESS
                               -> FAILED
                               -> PENDING_RECONCILIATION
PENDING_RECONCILIATION --------> SUCCESS | FAILED
```

The three outcome states are `SUCCESS`, `FAILED`, and
`PENDING_RECONCILIATION`; pending remains unresolved and can later transition.
`RESERVED` and `PAYOUT_IN_PROGRESS` are persisted recovery states. The status
endpoint may expose either transient state during processing or after a crash;
they are not additional final outcomes.

## Request and transaction flow

1. Validate the request and build a canonical fingerprint. Return an existing
   matching idempotency key immediately unless it is recoverable `RESERVED`;
   reject a conflicting fingerprint.
2. Read one completed rate snapshot and calculate the selected quote.
3. In a short `READ COMMITTED` transaction, insert the settlement and quote
   under the owner/idempotency unique constraint, lock affected accounts with
   `SELECT FOR UPDATE` in ascending account-ID order, verify funds, write the
   reservation journal, update balances, and commit `RESERVED`. A concurrent
   insertion loser rolls back, reloads the winner, and does no financial work.
4. Lock the settlement and conditionally transition `RESERVED` to
   `PAYOUT_IN_PROGRESS`. Only the winner may invoke the provider. A replay of a
   crash-left `RESERVED` row participates in the same claim.
5. Call the provider outside database transactions using the settlement ID as
   its idempotency key.
6. In a new transaction, lock and recheck the settlement and account rows:
   - `200`: consume the reservation and set `SUCCESS`.
   - `503`: release it and set `FAILED`.
   - Timeout: retain it and set `PENDING_RECONCILIATION`.
7. Reconciliation looks up the same operation. Paid consumes the reserve,
   unpaid releases it, and unknown leaves it pending.

Every transition is conditional on the current state. Settlement row locking
and the unique journal event prevent concurrent callbacks or reconcilers from
posting twice. Account locks serialize spend against one balance without
holding locks during the five-second provider timeout.

If recovery finds `PAYOUT_IN_PROGRESS`, it first queries the provider. It may
submit with the same idempotency key only when the provider definitively reports
that no operation exists. It never resubmits an ambiguous operation.

The provider operation ID is the settlement UUID, assigned and persisted when
the settlement row is created. It is stable across replay and recovery.

## API

- `POST /settlements`: create or replay an idempotent settlement.
- `GET /settlements/{settlement_id}`: return status, quote, route, and reserve
  disposition.
- `POST /settlements/{settlement_id}/reconcile`: query and apply an existing
  provider operation's definitive result.
- `GET /health`: execute PostgreSQL `SELECT 1`; return `200` when ready and
  `503` when the database is unavailable.

## Deployment and verification

Docker Compose runs the API and PostgreSQL, waits for database readiness, runs
migrations and idempotent seed data, and starts the rate publisher with the
FastAPI lifecycle. Provider outcomes can be deterministic during tests.

The 1,000-request load test verifies balances, balanced journals, unique
settlement effects, released failed amounts, retained pending amounts, and
exactly-once consumption of successful amounts. At 10,000 RPS, hot account
rows, the database pool/WAL, synchronous provider waits, and per-process rate
snapshots become the primary bottlenecks.
