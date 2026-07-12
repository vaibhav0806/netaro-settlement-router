# Netaro Settlement Router

Netaro Router is a FastAPI/PostgreSQL implementation of an atomic FX
settlement loop. It selects the route that maximizes the receiver's output,
reserves customer USD through a double-entry ledger, calls an idempotent mock
payout provider without holding database locks, and durably reconciles
ambiguous provider outcomes without risking a duplicate payout.

## Important files

| File | Purpose |
|---|---|
| [SPEC.md](SPEC.md) | Original assignment and evaluation criteria |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, request flow, data model, state transitions, concurrency boundaries, and scaling design |
| [ADR.md](ADR.md) | Accepted decisions for routing, database locking, ledger design, payout failure handling, and 10,000 RPS evolution |
| [AUDIT.md](AUDIT.md) | Recorded unit, integration, Docker E2E, load-test, and ledger-invariant results with rerun commands |
| [Settlement design](docs/superpowers/specs/2026-07-12-settlement-router-design.md) | Reviewed implementation design and assignment assumptions |
| [Implementation plan](docs/superpowers/plans/2026-07-12-netaro-settlement-router.md) | Original TDD implementation sequence and requirement mapping |
| [Docker E2E design](docs/superpowers/specs/2026-07-12-docker-e2e-audit-design.md) | Reviewed isolation, scenario, lifecycle, artifact, and audit design |
| [Docker E2E plan](docs/superpowers/plans/2026-07-12-docker-e2e-audit.md) | End-to-end, lifecycle, concurrency, evidence, and audit plan |
| [Application](app/) | FastAPI service, routing engine, payout provider, ledger, models, and settlement state machine |
| [Database migrations](alembic/versions/) | PostgreSQL schema, ledger guards, payout-attempt persistence, indexes, and seeded accounts |
| [Automated tests](tests/) | Unit, PostgreSQL integration, isolated Docker E2E, and canonical load proof |
| [Docker E2E runner](tests/e2e/run.py) | Fresh-project orchestration, health checks, lifecycle tests, invariant audit, redacted artifacts, and cleanup |
| [1,000-request load proof](tests/load_test.py) | Concurrent HTTP workload followed by a read-only accounting and routing audit |

## Architecture summary

```text
Client
  -> FastAPI validation and owner-scoped idempotency
  -> immutable in-memory FX snapshot lookup
  -> short PostgreSQL transaction
       settlement row + SELECT FOR UPDATE + reserve journal
  -> commit before external I/O
  -> mock payout provider using settlement UUID as operation ID
  -> short PostgreSQL transaction
       consume, release, or preserve reservation
  -> SUCCESS | FAILED | PENDING_RECONCILIATION
```

Key correctness properties:

- "Cheapest" means the maximum amount received in the target currency.
- Bellman-Ford-style relaxation builds deterministic maximum-product routes
  and rejects reachable profitable cycles. Snapshot construction is `O(VE)`;
  serving a published quote is `O(1)` plus route materialization.
- Rates publish as immutable snapshots every 50 ms. A settlement stores the
  exact snapshot version, route, LPs, rates, aggregate rate, and quoted output.
- Customer funds move between USD `AVAILABLE` and `RESERVED` accounts using
  balanced double-entry journals. Successful payouts consume the reservation;
  authoritative unpaid results release it. PostgreSQL deferred triggers reject
  unbalanced journals and prevent changes to posted journals and postings.
- The owner/idempotency-key database constraint and request fingerprint make
  replay safe. Equivalent decimal inputs replay the original result; changed
  payloads return `409`.
- Account rows are locked in a deterministic order using `SELECT FOR UPDATE`.
  Locks are held only in short database transactions, never during provider
  calls.
- A payout timeout or transport failure is ambiguous. Funds remain reserved and the settlement
  becomes `PENDING_RECONCILIATION`; the service does not immediately retry.
  A leased background reconciler queries the original durable provider
  operation. Attempt tokens fence stale workers from applying an outcome.
- The health endpoint checks PostgreSQL and returns `200` with
  `{"status":"ok"}` or `503` while the database is unavailable.

The assignment intentionally ignores LP fees, liquidity capacity, slippage,
and quote expiry. The ledger uses USD as its accounting currency; target
currency values remain quote and audit metadata.

## Audit summary

The full evidence, exact source revision, and coverage matrix are in
[AUDIT.md](AUDIT.md).

| Verification lane | Recorded result |
|---|---:|
| Unit tests | 42 passed |
| PostgreSQL integration tests | 87 passed |
| Combined unit and integration | 129 passed |
| Isolated Docker E2E scenarios | 5 passed |
| Canonical load workload | 1,000/1,000 requests completed |
| Load duration | 11.76 seconds |
| Final provider outcomes | 850 success / 150 failed / 0 pending |
| Routing snapshots exercised | 59 |
| Ledger journals | 1 opening + 2,000 settlement journals |
| Final available/reserved USD | 15,000 / 0 |
| Negative accounts | 0 |
| Unbalanced journal/currency groups | 0 |
| Duplicate owner/key or settlement/event rows | 0 |

## Prerequisites

- Docker with Docker Compose v2
- Python 3.12 for running tests from the host
- Ports `8000` and `5432` available for the simplest Docker startup, or custom
  `API_HOST_PORT` and `POSTGRES_HOST_PORT` values

## Run the application

Build the image, migrate PostgreSQL, seed `demo-customer` with USD 100,000, and
start the API:

```bash
docker compose up --build
```

In another terminal, wait for readiness:

```bash
curl --fail http://localhost:8000/health
```

Expected response:

```json
{"status":"ok"}
```

The deterministic mock provider initially produces 70% paid, 15% ambiguous
`503`, and 15% five-second timeout outcomes over each group of 20 unique
operations. Its operations are stored in PostgreSQL. Reconciliation eventually
resolves ambiguous operations to an exact 50/50 paid/unpaid split.

To stop the application while preserving its database volume:

```bash
docker compose down
```

Use `docker compose down -v` only when a clean database is required.

## Exercise the API

Both `Idempotency-Key` and `X-Owner-ID` are required when creating a
settlement. Supported target currencies are `USDC`, `EUR`, `PHP`, and `AED`.

```bash
curl --fail --request POST http://localhost:8000/settlements \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: example-001' \
  --header 'X-Owner-ID: demo-customer' \
  --data '{"amount_usd":"100","target_currency":"PHP"}'
```

Use the returned settlement UUID to read its durable state:

```bash
curl --fail http://localhost:8000/settlements/<settlement-uuid>
```

Request immediate reconciliation of an ambiguous operation:

```bash
curl --fail --request POST \
  http://localhost:8000/settlements/<settlement-uuid>/reconcile
```

Reconciliation is also automatic. Manual reconciliation is idempotent, queries
the existing operation, and never creates a second payout. Terminal settlements
are returned unchanged. Create and reconcile return `202` while an operation is
still pending and `200` for a terminal result.

## Install host test dependencies

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

The commands below use PostgreSQL port `55432` so the test database does not
conflict with another local PostgreSQL instance.

## Run unit and PostgreSQL integration tests

Start only the database and apply the production migration:

```bash
POSTGRES_HOST_PORT=55432 docker compose up -d db
DATABASE_URL=postgresql+asyncpg://netaro:netaro@localhost:55432/netaro \
  .venv/bin/alembic upgrade head
```

Run the complete default suite:

```bash
POSTGRES_HOST_PORT=55432 \
  .venv/bin/pytest tests/unit tests/integration -q
```

Or run the lanes separately:

```bash
.venv/bin/pytest tests/unit -q
POSTGRES_HOST_PORT=55432 .venv/bin/pytest tests/integration -q
```

The integration fixtures create and destroy a separate `netaro_test` database.

Run formatting, lint, static typing, and bytecode compilation:

```bash
.venv/bin/ruff format --check app tests
.venv/bin/ruff check app tests
.venv/bin/mypy app
.venv/bin/python -m compileall -q app tests
```

## Run isolated Docker E2E scenarios

Each scenario selects unused host ports, builds the application, starts a
fresh Compose project and volume, waits for exact health readiness, captures
redacted evidence, and always removes its containers and volume.

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

The scenarios cover:

- API validation, disabled docs, stable error contracts, and health behavior;
- quote arithmetic, changing rate snapshots, replay, conflicts, and 100
  concurrent requests using the same idempotency key;
- the exact 14 successful/6 pending initial provider distribution and eventual
  17 successful/3 failed terminal distribution across 20 concurrent requests;
- 101 concurrent USD 1,000 settlements against USD 100,000 without overspend;
- API restart persistence, durable provider lookup, automatic reconciliation,
  database outage and recovery;
- read-only checks for negative accounts, unbalanced journals, duplicate
  owner/key rows, and duplicate settlement/event rows.

Evidence is written to `.artifacts/e2e/<run-id>/` and is intentionally ignored
by Git. See [AUDIT.md](AUDIT.md) for the recorded run IDs.

## Run the exact 1,000-request proof

The canonical proof is destructive to its selected Compose volume. It uses a
dedicated project name and ports below, leaving other Compose projects alone.

```bash
API_HOST_PORT=58080 POSTGRES_HOST_PORT=55433 PAYOUT_MODE=load \
  docker compose -p netaro-load-audit up --build -d

until curl --fail --silent http://127.0.0.1:58080/health >/dev/null; do
  sleep 0.5
done

POSTGRES_HOST_PORT=55433 .venv/bin/python tests/load_test.py \
  --base-url http://127.0.0.1:58080 \
  --requests 1000 \
  --concurrency 1000 \
  --amount 100

API_HOST_PORT=58080 POSTGRES_HOST_PORT=55433 \
  docker compose -p netaro-load-audit down -v --remove-orphans
```

The load program sends 1,000 unique HTTP requests concurrently, waits for all
ambiguous timeouts, and audits PostgreSQL in a read-only transaction. It fails
nonzero on any status distribution, balance, journal, idempotency,
provider-operation, or routing mismatch. Every stored route is regenerated
from its snapshot version and checked for exact receiver output.

Expected final line:

```text
PASS settlements=1000 success=850 failed=150 pending=0 settlement_journals=2000 available_usd=15000 reserved_usd=0
```

## Scope and production limits

This submission prioritizes routing correctness, atomic reservation,
double-entry accounting, concurrency control, idempotency, and ambiguous
failure safety within the assignment's four-hour boundary.

It does not implement real LP or payout integrations, LP fees, liquidity
capacity, slippage, quote expiry, target-currency ledger valuation,
authentication/authorization, customer-account provisioning, a durable payout
queue/outbox, distributed rate
snapshots, horizontal worker partitioning, or production observability.

The included mock provider persists operations locally in PostgreSQL to make
restart and reconciliation behavior testable; a real deployment still needs a
durable external provider operation/query API. The local Docker load result is
a correctness proof, not a claim of production throughput. The proposed path
to 10,000 RPS is documented in [ARCHITECTURE.md](ARCHITECTURE.md) and
[ADR.md](ADR.md).
