# Verification Audit

## Result

The implementation passed all automated verification performed on
`9baac53d8b425a98c7b4a46b57ff40ea24e57044` on 12 July 2026 (Asia/Kolkata).
The repository was clean for the recorded Docker E2E runs.

| Lane | Result | Evidence |
|---|---:|---|
| Unit tests | 40 passed | Routing, provider, schemas, and E2E runner safety |
| PostgreSQL integration tests | 78 passed | API, ledger, constraints, locking, idempotency, settlement state machine |
| Isolated Docker E2E | 5 passed | Fresh Compose project and volume per scenario |
| Canonical load proof | Passed | 1,000 HTTP requests at concurrency 1,000 |
| Total unit + integration | 118 passed in 36.13s | Final clean run |

## Docker E2E scenarios

| Scenario | Assertions exercised | Result | Artifact run ID |
|---|---|---:|---|
| `boot-contract` | Exact health response; docs disabled; missing/empty headers; malformed JSON and UUID; zero, negative, over-precision, oversized and unsupported-currency inputs; insufficient funds; missing settlement | Pass | `20260711T230810Z-8b4ff696` |
| `settlement-idempotency` | Quote arithmetic and contiguous route; GET; equivalent decimal replay; changed amount/currency conflict; 100 concurrent replays produce one settlement/provider operation | Pass | `20260711T230818Z-bb30bf16` |
| `provider-reconciliation` | 20 concurrent calls produce exactly 14 success, 3 failed and 3 ambiguous; terminal reconcile is a no-op; repeated ambiguous reconciliation remains pending | Pass | `20260711T230826Z-adad421e` |
| `concurrency-funds` | 101 concurrent requests for USD 1,000 against USD 100,000 produce 100 successes and one 409; no negative account, unbalanced journal, duplicate owner/key, or duplicate settlement/event | Pass | `20260711T230839Z-d1af58fd` |
| `lifecycle-recovery` | Terminal and pending rows survive API restart; provider-memory loss does not retry an ambiguous payout; database outage returns health 503; recovery returns 200; ledger invariants remain valid | Pass | `20260711T230848Z-3fd20a65` |

Each run captured redacted metadata, resolved Compose configuration, readiness
history, container/image identity, API and database logs, scenario output,
read-only audit output where applicable, and teardown output under
`.artifacts/e2e/<run-id>/`. These artifacts are intentionally gitignored.

## Canonical 1,000-request proof

The final run used a fresh PostgreSQL volume, `PAYOUT_MODE=load`, 1,000 unique
idempotency keys, concurrency 1,000, and USD 100 per request.

| Measurement | Observed | Required |
|---|---:|---:|
| Completed HTTP requests | 1,000 | 1,000 |
| Elapsed request time | 10.08s | Informational |
| Approximate throughput | 99.21 requests/s | Informational |
| Distinct routing snapshots | 47 | At least 2 |
| `SUCCESS` | 700 | 700 |
| `FAILED` | 150 | 150 |
| `PENDING_RECONCILIATION` | 150 | 150 |
| Settlement journals | 1,850 | 1,850 |
| Opening journals | 1 | 1 |
| Available USD | 15,000 | 15,000 |
| Reserved USD | 15,000 | 15,000 |

The load audit also regenerated every stored route from its snapshot version
and checked the path, LP, rates, aggregate rate, and receiver output. Its
read-only transaction verified unique provider operation IDs, balanced
double-entry journals, no negative accounts, no duplicate owner/idempotency
keys or settlement/events, and USD 70,000 of successful omnibus movement.

## Edge-case coverage summary

| Area | Coverage |
|---|---|
| Routing | Maximum receiver output, deterministic ties, changing snapshots, disconnected graph, invalid rates, profitable cycles including disconnected cycles, publication/read concurrency |
| Money and ledger | Decimal precision, reserve/consume/release, positive and balanced postings, journal structure, exact exhaustion, mixed-amount contention, rollback, no negative balances |
| Idempotency | Owner-scoped key, equivalent decimals, conflicting payloads, concurrent identical and conflicting requests, one provider operation per settlement |
| State machine | Paid, unpaid, timeout, unexpected provider results, repeated/parallel reconciliation, terminal no-op, provider-memory loss after restart |
| Operations | Fresh migrations, health readiness, disabled API docs, isolated ports/projects/volumes, API restart, database outage and recovery, unconditional cleanup and redacted evidence |

## Issues found while extending verification

| Issue | Resolution |
|---|---|
| Lifecycle test could observe a connection reset while the API was still restarting instead of the intended database-down 503 | Runner now waits for post-restart readiness before stopping PostgreSQL, then polls for the explicit 503 and verifies recovery to 200 |
| Initial E2E data used an owner without provisioned ledger accounts | Tests now use the seeded `demo-customer`, matching the runnable assignment contract |

## Known limits

- The public API assumes customer ledger accounts are provisioned out of band;
  this assignment exposes no customer/account provisioning endpoint.
- Routing snapshot construction is Bellman-Ford-style `O(VE)`, deliberately
  differing from the specification's `O(V+E)` target so cyclic maximum-product
  graphs and profitable-cycle rejection remain correct. Published quote lookup
  is `O(1)` plus route materialization.
- The mock provider is process-local. After a restart, an ambiguous operation
  remains safely reserved and pending, but real reconciliation requires a
  durable provider operation/query API.
- The load result is a local Docker correctness proof, not a production
  capacity benchmark. Host hardware and Docker scheduling affect throughput.

## Rerun commands

```bash
POSTGRES_HOST_PORT=55432 docker compose up -d db
POSTGRES_HOST_PORT=55432 .venv/bin/pytest tests/unit tests/integration -q
```

```bash
for scenario in \
  boot-contract \
  settlement-idempotency \
  provider-reconciliation \
  concurrency-funds \
  lifecycle-recovery
do
  .venv/bin/python tests/e2e/run.py --scenario "$scenario" || exit $?
done
```

```bash
API_HOST_PORT=58080 POSTGRES_HOST_PORT=55433 PAYOUT_MODE=load \
  docker compose -p netaro-load-audit up --build -d
POSTGRES_HOST_PORT=55433 .venv/bin/python tests/load_test.py \
  --base-url http://127.0.0.1:58080 \
  --requests 1000 --concurrency 1000 --amount 100
API_HOST_PORT=58080 POSTGRES_HOST_PORT=55433 \
  docker compose -p netaro-load-audit down -v --remove-orphans
```
