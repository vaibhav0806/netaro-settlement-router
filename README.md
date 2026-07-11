# Netaro Settlement Router

Netaro Router is a FastAPI/PostgreSQL service that selects the greatest-output
FX route, reserves customer USD, and settles through an idempotent payout
provider without holding database locks during provider I/O.

The detailed design is in [ARCHITECTURE.md](ARCHITECTURE.md), and the accepted
locking, routing, and failure decisions are in [ADR.md](ADR.md).

## Quick start

Docker Compose migrates the database, seeds `demo-customer` with USD 100,000,
and starts the API:

```bash
docker compose up --build
```

In another terminal, check readiness:

```bash
curl --fail http://localhost:8000/health
```

The health response is `{"status":"ok"}`. The default demo payout provider
uses a repeatable 70% paid, 15% definitively unpaid, and 15% timeout mix.

## API examples

Create a settlement. Both `Idempotency-Key` and `X-Owner-ID` are required:

```bash
curl --fail --request POST http://localhost:8000/settlements \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: example-001' \
  --header 'X-Owner-ID: demo-customer' \
  --data '{"amount_usd":"100","target_currency":"PHP"}'
```

Use the returned UUID to read status or reconcile an ambiguous timeout:

```bash
curl --fail http://localhost:8000/settlements/<settlement-uuid>
curl --fail --request POST \
  http://localhost:8000/settlements/<settlement-uuid>/reconcile
```

Reconciliation performs provider lookup only for an existing ambiguous
operation. It does not create a second payout.

## Local development and tests

Python 3.12 is required. Install the application and test dependencies, then
run migrations and tests against PostgreSQL:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
POSTGRES_HOST_PORT=55432 docker compose up -d db
DATABASE_URL=postgresql+asyncpg://netaro:netaro@localhost:55432/netaro \
  .venv/bin/alembic upgrade head
DATABASE_URL=postgresql+asyncpg://netaro:netaro@localhost:55432/netaro \
  .venv/bin/pytest tests/unit -q
DATABASE_URL=postgresql+asyncpg://netaro:netaro@localhost:55432/netaro \
  .venv/bin/pytest tests/integration -q
```

Port `55432` keeps this project stack isolated from any PostgreSQL already on
the default host port. Override `POSTGRES_HOST_PORT` consistently if needed.

## Isolated Docker end-to-end tests

Each scenario builds the application, selects unused host ports, starts a
fresh Compose project and volume, captures redacted evidence under
`.artifacts/e2e/`, and always tears the project down:

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

The lifecycle scenario restarts the API, verifies terminal and ambiguous
settlements survive, confirms an ambiguous payout is not retried, and checks
that `/health` changes from `503` during a database outage back to `200` after
recovery. Concurrency scenarios also run read-only ledger invariant queries.

## Exact 1,000-request proof

The load proof is destructive to this project's Compose volume. It requires a
clean volume so the opening journal and all settlement totals are exact. The
`load` payout mode is explicit; normal startup never selects it.

```bash
POSTGRES_HOST_PORT=55432 docker compose down -v
POSTGRES_HOST_PORT=55432 PAYOUT_MODE=load docker compose up --build -d
curl --fail http://localhost:8000/health
POSTGRES_HOST_PORT=55432 .venv/bin/python tests/load_test.py \
  --base-url http://localhost:8000 \
  --requests 1000 \
  --concurrency 1000 \
  --amount 100
```

The runner sends 1,000 unique HTTP requests concurrently, waits for the 150
five-second timeouts, and then opens a read-only PostgreSQL transaction. It
fails nonzero on any status, journal, balance, idempotency, provider-operation,
or routing mismatch. Every stored route is regenerated from
`generate_edges(snapshot_version)` and checked for the exact path, LPs, rates,
and receiver output; at least two snapshot versions must be present. A valid
run ends with:

```text
PASS settlements=1000 success=700 failed=150 pending=150 settlement_journals=1850 available_usd=15000 reserved_usd=15000
```

## Routing and assumptions

Each immutable rate snapshot precomputes maximum-product USD routes with
Bellman-Ford-style relaxation. Snapshot construction is `O(VE)`. This is the
accepted correctness tradeoff for a cyclic multiplicative graph; request-time
target lookup is `O(1)` plus at most `O(V)` route hops. The rationale and
rejected BFS/Dijkstra alternatives are recorded in [ADR.md](ADR.md).

The ledger is USD-only. Target-currency values and routes are quote/audit
metadata, not multi-currency postings. A provider `503` is treated as a
definitive unpaid result and releases the reservation. A timeout is ambiguous:
funds stay reserved in `PENDING_RECONCILIATION`, no automatic retry occurs, and
reconciliation looks up the original provider operation ID.

## Four-hour scope boundary

This submission does not implement real LP or payout integrations, fees,
liquidity capacity, slippage, quote expiry, target-currency ledger valuation,
authentication/authorization, a durable payout queue/outbox, distributed rate
snapshots, horizontal worker partitioning, observability infrastructure, or a
production reconciliation scheduler. The 10,000 RPS evolution is documented
as architecture only; it is not claimed as implemented throughput.

## Submission checklist

- [ ] Add the repository URL to the submission form.
- [ ] Add the unedited recording URL; do not substitute an edited demo.
- [ ] Attach or paste the exact 1,000-request PASS output.
- [ ] Link the accepted [ADR.md](ADR.md).
- [ ] Confirm the recording and repository were produced within the four-hour window.
