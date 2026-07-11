# Docker E2E and Verification Audit Design

## Objective

Extend the existing unit, PostgreSQL integration, and canonical load coverage
with a repeatable black-box Docker E2E suite. The suite must start the real API
and PostgreSQL containers from clean project-scoped volumes, exercise only
public HTTP behavior in its black-box lane, run separate internal ledger
audits, preserve diagnostic artifacts, and produce an evidence-backed
`AUDIT.md` with exact rerun commands.

The work is test-focused. It must not add authentication, queues, production
reset endpoints, test-only HTTP controls, or new settlement behavior merely to
make E2E orchestration easier.

## Evidence lanes

The report and artifacts distinguish five lanes:

- `UNIT`: pure routing, provider, and test-runner behavior.
- `INTEGRATION`: application services against real PostgreSQL.
- `BLACK_BOX_E2E`: HTTP-only assertions against the Dockerized application.
- `INTERNAL_SQL_AUDIT`: direct read-only validation of ledger/database state.
- `LOAD`: timed concurrent HTTP execution followed by explicit invariants.

SQL observations must never be presented as black-box API evidence. The
existing canonical load runner is a hybrid `LOAD` plus
`INTERNAL_SQL_AUDIT` lane because it imports the deterministic rate generator
and queries PostgreSQL directly.

## Docker isolation and lifecycle

`tests/e2e/run.py` owns Docker lifecycle for every run:

1. Create a unique Compose project name such as `netaro-e2e-<run-id>`.
2. Select available API and PostgreSQL host ports through
   `API_HOST_PORT` and `POSTGRES_HOST_PORT`.
3. Start from a new project-scoped volume with `docker compose -p <project>
   up --build -d`.
4. Poll the public `/health` endpoint until it returns exact readiness; never
   depend on a fixed sleep.
5. Run the appropriate black-box scenario group.
6. Run a read-only SQL audit where that scenario needs internal financial
   proof.
7. Capture metadata, request results, container state, logs, and test output
   before teardown.
8. Always run `docker compose -p <project> down -v --remove-orphans` in a
   `finally` path without replacing the original test exit code.

The runner must always pass `-p`; it must never run an unqualified destructive
Compose command. `docker-compose.yml` will parameterize the API host mapping as
`${API_HOST_PORT:-8000}:8000`. Existing developer projects and volumes are out
of scope and must not be stopped, reused, or deleted.

## Black-box test client

`tests/e2e/test_docker_api.py` receives `E2E_BASE_URL` and uses HTTP/JSON only.
It must not import `app.*`, use SQLAlchemy/asyncpg, or inspect container files.
It validates response status, body schema, UUIDs, Decimal arithmetic, route
continuity, idempotency, and observable lifecycle behavior.

Rate correctness has two levels:

- Black-box tests verify that hops start at USD, are contiguous, end at the
  target, multiply exactly to the returned aggregate rate, and produce the
  quoted amount at eight-decimal persistence precision.
- A test-local, independent brute-force simple-path oracle reconstructs the
  documented versioned three-LP graph without importing production routing.
  It compares maximum receiver output for selected snapshots and targets.

## Scenario groups

### 1. Boot and API contract

- Build and start from an empty volume.
- Prove migrations and balanced seed finish before `/health` becomes ready.
- Assert health is exactly `200 {"status": "ok"}`.
- Assert only the four approved endpoint paths are reachable.
- Assert `/docs`, `/redoc`, and `/openapi.json` return 404.
- Cover missing/empty headers, malformed JSON, zero/negative/overprecision and
  oversized amounts, unsupported currency, malformed UUID, and absent UUID.
- Assert stable 404, 409, 422, and 503 bodies where specified.

### 2. Settlement, quote, and idempotency

- Create, GET, and replay one settlement.
- Validate response schema, exact Decimal values, route continuity, aggregate
  rate, and receiver output.
- Replay numerically equivalent amounts such as `100.0` and `100.00` and
  receive the original settlement/quote.
- Reuse the key with changed amount and changed target; both return 409.
- Send 100 concurrent identical requests; all return the same settlement ID
  and semantic response.
- Verify at least two rate snapshot versions across a paced request group.

### 3. Provider outcomes and reconciliation

On a fresh default-provider project, submit exactly 20 unique settlements and
assert 14 `SUCCESS`, 3 `FAILED`, and 3 `PENDING_RECONCILIATION`. The three
timeouts must complete within one generous concurrent wave rather than three
serial five-second waits.

- Reconciling terminal settlements is a no-op.
- Repeated reconciliation of the default provider's unknown timed-out
  operations remains pending with the same reservation state.
- No second public payout initiation is observable.

Definitive paid/unpaid reconciliation remains an integration-test concern;
the production API will not gain test-only provider controls.

### 4. Concurrency and funds

On a fresh `load` provider project, send 101 concurrent USD 1,000 settlements
against the seeded USD 100,000 balance. The first 101 load-provider operations
are paid, so exactly 100 requests succeed and one returns 409 insufficient
funds. The SQL audit proves available/reserved balances never become negative,
all financial events are unique, and every journal remains balanced.

Integration additions also cover:

- Same idempotency key scoped independently to two owners.
- Concurrent same-key requests with conflicting payloads.
- Concurrent unpaid reconciliation/release.
- Repeated `UNKNOWN` reconciliation.
- Mixed-amount overspend at the exact boundary.

### 5. Lifecycle and recovery

- Create a terminal settlement, restart only the API container against the
  same volume, and prove GET/replay returns the existing settlement without a
  new database effect.
- Create a pending settlement, restart the API, and prove the persisted
  reservation remains safe. Record that the mock provider's in-process
  operation state is lost, so the pending outcome cannot become definitive
  after restart without a durable external provider.
- Stop PostgreSQL and poll until `/health` returns exact 503; restart it and
  poll until exact 200 recovery.
- Capture API/DB logs for startup, restart, outage, and recovery.

### 6. Canonical load proof

Retain the clean-volume 1,000-request proof with concurrency 1,000 and USD 100:

- Exactly 700 success, 150 definitive failure, and 150 pending outcomes.
- Exactly 1,000 reserves, 700 consumes, 150 releases, and one opening journal.
- Exact customer available/reserved and Omnibus movements.
- No negative account, unbalanced journal, duplicate key, or duplicate event.
- Exactly 1,000 provider operation IDs matching settlement IDs.
- Every stored route matches its versioned graph and receiver output.
- At least two snapshot versions are observed.

Extend timing output with throughput plus p50, p95, p99, and maximum latency.
The proof remains fixed at `(requests=1000, amount=100)`; separate smoke and
hot-key scenarios cover smaller concurrency behavior.

## Additional unit and integration coverage

Add focused tests for uncovered high-value behavior without duplicating E2E:

- Invalid rate publication does not replace the last valid snapshot.
- Concurrent snapshot readers never observe mixed versions.
- An unreachable profitable cycle does not invalidate an unrelated target.
- Positive-but-incorrect materialized balance is detected.
- Posting amount/currency constraints and exact two-posting journal structure.
- Same key for two owners and concurrent conflicting-payload idempotency.
- Repeated unknown and concurrent unpaid reconciliation.
- Unexpected provider initiation/lookup exceptions leave recoverable persisted
  state and do not lose reserved funds.

If a new test exposes a production defect, fix it through a separate
test-first RED/GREEN cycle. Tests should document intentionally safe but
unresolved behavior rather than inventing unsupported production features.

## Artifacts

Each run writes ignored artifacts under `.artifacts/e2e/<run-id>/`:

- `metadata.txt`: UTC start/end, Git SHA/dirty state, host platform, Docker and
  Compose versions, Compose project, selected ports.
- `compose-config.yml`: resolved configuration with secrets redacted.
- `images.txt`, `compose-ps.txt`, and volume/container identifiers.
- `readiness.log` and scenario runner stdout/stderr.
- `requests.jsonl` with request ID, scenario, status, settlement ID, latency,
  and error; never include credentials.
- `pytest.xml` and concise scenario summary.
- `latency.json` with count, throughput, p50, p95, p99, and maximum.
- `internal-audit.txt` with SQL assertions/results.
- `api.log`, `db.log`, and `teardown.txt`, captured even on failure.

Artifacts are diagnostic evidence and are not committed. `AUDIT.md` links the
artifact paths from the verified run.

## AUDIT.md

After all suites run, create root `AUDIT.md` containing:

1. Executive outcome and any failed/blocked scenarios.
2. Run metadata table: run ID, Git SHA, UTC start/end, platform, Python,
   PostgreSQL, Docker/Compose, image IDs, and total duration.
3. Summary table by evidence lane with passed/failed/skipped counts and time.
4. Requirement/scenario table:

```text
| ID | Lane | Requirement | Scenario | Expected | Actual | Status | Duration | Evidence | Notes |
```

5. Exact accounting and load-result table.
6. Known limitations and intentionally untested production-scale concerns.
7. Exact rerun commands for unit, integration, Docker E2E, internal SQL audit,
   and canonical load proof.

Results must come from the actual final run at the recorded tested-source Git
SHA. `AUDIT.md` is added in the following documentation-only commit and must
state that distinction explicitly. Any later production or test-code change
invalidates the evidence and requires a rerun; `AUDIT.md` must then be updated
rather than claiming stale results are current.

## Completion criteria

- Unit and PostgreSQL integration suites pass with no skipped correctness
  tests.
- Every Docker black-box group passes from a clean isolated project.
- Internal SQL audit passes and remains clearly labeled.
- Canonical 1,000-request proof passes with exact totals and latency metrics.
- Failure paths preserve logs/artifacts and return nonzero.
- `AUDIT.md` accurately summarizes actual commands/results and known gaps.
- `git diff --check`, package installation, migrations, and Docker build pass.
