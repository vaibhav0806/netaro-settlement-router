# Verification Audit

## Result

The implementation passed the complete verification suite on source commit
`e3339295c91d469ba3f05e24dc1ffd2bd07f8de7` on 12 July 2026
(Asia/Kolkata). Every recorded Docker E2E run reports `dirty=false`.

| Lane | Result |
|---|---:|
| Unit tests | 42 passed in 15.41s |
| PostgreSQL integration tests | 87 passed in 24.30s |
| Total unit and integration tests | 129 passed |
| Isolated Docker E2E scenarios | 5 passed |
| Canonical load proof | 1,000/1,000 requests passed |
| Ruff formatting and lint | Passed |
| Strict mypy | Passed, 11 source files |
| Python bytecode compilation | Passed |

## Docker E2E scenarios

| Scenario | Assertions exercised | Artifact run ID |
|---|---|---|
| `boot-contract` | Health, disabled docs, stable error contracts, malformed inputs, amount bounds, unsupported currency, insufficient funds, and missing settlement | `20260712T002621Z-48919e6a` |
| `settlement-idempotency` | Quote arithmetic, route continuity, equivalent replay, payload conflicts, and 100 concurrent replays producing one settlement/provider operation | `20260712T002631Z-373153de` |
| `provider-reconciliation` | Exact initial 14 success/6 pending distribution, eventual 17 success/3 failed terminal distribution, and terminal reconciliation no-op | `20260712T002638Z-a35996c0` |
| `concurrency-funds` | 101 concurrent USD 1,000 requests against USD 100,000 without overspend, negative balances, duplicate effects, or unbalanced journals | `20260712T002652Z-b9735c01` |
| `lifecycle-recovery` | Durable provider state and automatic reconciliation across API restart, database-down health `503`, recovery to `200`, and ledger invariants | `20260712T002701Z-486b5f92` |

Each run used a fresh Compose project and PostgreSQL volume, captured redacted
configuration, readiness history, container identity, API/database logs,
scenario output, read-only accounting audit, and teardown output under
`.artifacts/e2e/<run-id>/`. These local artifacts are gitignored.

## Canonical 1,000-request proof

The final run used a fresh PostgreSQL volume, deterministic load payout mode,
1,000 unique idempotency keys, concurrency 1,000, and USD 100 per request.

| Measurement | Observed | Required |
|---|---:|---:|
| Completed HTTP requests | 1,000 | 1,000 |
| Elapsed request time | 11.13s | Informational |
| Approximate throughput | 89.85 requests/s | Informational |
| Distinct routing snapshots | 54 | At least 2 |
| Initial `SUCCESS` | 700 | 700 |
| Initial pending reconciliation | 300 | 300 |
| Final `SUCCESS` | 850 | 850 |
| Final `FAILED` | 150 | 150 |
| Final `PENDING_RECONCILIATION` | 0 | 0 |
| Settlement journals | 2,000 | 2,000 |
| Opening journals | 1 | 1 |
| Available USD | 15,000 | 15,000 |
| Reserved USD | 0 | 0 |

The audit regenerated every stored route from its snapshot version and checked
the path, LP, rates, aggregate rate, and receiver output. Its read-only database
checks found zero negative accounts, unbalanced journals, unposted journals,
duplicate owner/idempotency keys, duplicate settlement/events, or duplicate
provider operation IDs.

## Coverage summary

| Area | Verified behavior |
|---|---|
| Routing | Maximum receiver output, deterministic ties, disconnected targets, invalid/non-finite rates, reachable profitable cycles, changing immutable snapshots, publication/read concurrency, and exhaustive simple-path oracle comparison |
| Decimal safety | Isolated 38-digit half-even context and independence from process-global Decimal settings |
| Ledger | Reserve/consume/release, exact exhaustion, rollback, deterministic account locking, database-enforced balancing, minimum posting structure, posted-journal immutability, and nonnegative balances |
| Idempotency | PostgreSQL owner/key uniqueness, canonical fingerprint, equivalent decimal replay, conflicting payload rejection, concurrent replay, and one durable provider operation per settlement |
| Provider safety | Database-backed operations, ambiguous timeout/503 handling, preserved reservations, stable operation IDs, no blind retry, automatic reconciliation, expiring leases, fencing tokens, and `SKIP LOCKED` claims |
| Operations | Production migrations in tests, connection release during provider I/O, health degradation and recovery, restart recovery, isolated Docker resources, unconditional cleanup, and redacted evidence |

## Known limits

- Customer ledger accounts are provisioned out of band; the assignment exposes
  no customer/account provisioning endpoint.
- Bellman-Ford snapshot construction is `O(VE)`, deliberately prioritizing
  correctness for a cyclic maximum-product graph. With the fixed five-currency
  graph it is effectively linear in `E`; published quote lookup is `O(1)` plus
  route materialization.
- The included database-backed provider is a deterministic test double. A real
  deployment still requires a durable external operation/query contract.
- The Docker load result is a local correctness and contention proof, not a
  production capacity benchmark.

## Rerun commands

```bash
POSTGRES_HOST_PORT=55432 docker compose up -d db
.venv/bin/pytest tests/unit -q
POSTGRES_HOST_PORT=55432 .venv/bin/pytest tests/integration -q
.venv/bin/ruff format --check app tests
.venv/bin/ruff check app tests
.venv/bin/mypy app
.venv/bin/python -m compileall -q app tests
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
until curl --fail --silent http://127.0.0.1:58080/health >/dev/null; do
  sleep 0.5
done
POSTGRES_HOST_PORT=55433 .venv/bin/python tests/load_test.py \
  --base-url http://127.0.0.1:58080 \
  --requests 1000 --concurrency 1000 --amount 100
API_HOST_PORT=58080 POSTGRES_HOST_PORT=55433 \
  docker compose -p netaro-load-audit down -v --remove-orphans
```
