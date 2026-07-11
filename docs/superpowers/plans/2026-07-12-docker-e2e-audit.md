# Docker E2E and Verification Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add comprehensive PostgreSQL integration and isolated Docker black-box E2E coverage, rerun the canonical 1,000-request proof with latency artifacts, and publish an evidence-backed `AUDIT.md`.

**Architecture:** A Python lifecycle runner creates a unique Docker Compose project and volume for each E2E scenario, polls public readiness, invokes HTTP-only pytest scenarios, captures artifacts, and tears down only its own resources. Black-box assertions remain separate from read-only PostgreSQL audits; the final audit documents actual results from a clean tested source commit.

**Tech Stack:** Python 3.12, pytest/pytest-asyncio, HTTPX, asyncpg, FastAPI, PostgreSQL 16, Docker Compose, JSONL/JUnit/Markdown evidence.

## Global Constraints

- Continue directly on `main`; do not create another worktree.
- Use strict TDD for every production or runner behavior change: observe RED before implementation, then GREEN, focused tests, full available suite, review, and commit.
- Coverage-only tests for behavior that already exists may pass immediately; report them honestly and do not fabricate RED evidence.
- Use real PostgreSQL for ledger, locking, state-machine, and reconciliation integration tests; never substitute SQLite.
- Black-box E2E tests use public HTTP only and must not import `app.*`, SQLAlchemy, asyncpg, or inspect container files.
- Direct database checks are labelled `INTERNAL_SQL_AUDIT`, run in read-only transactions, and are never presented as black-box evidence.
- Every Docker scenario uses a unique `docker compose -p <project>` name and project-scoped volume. Never run an unqualified destructive Compose command.
- Parameterize host ports; never stop, reuse, or delete another Compose project, container, database, or volume.
- Do not add production reset/admin endpoints, test-only provider controls, authentication, queues, outboxes, or new settlement behavior solely for testing.
- Preserve exact `Decimal`/PostgreSQL `NUMERIC` semantics and the existing documented Bellman-Ford, USD-ledger, 503, and timeout assumptions.
- Keep the canonical load proof fixed at 1,000 requests, concurrency 1,000, and USD 100 with exact 700/150/150 outcomes.
- Capture logs and artifacts on success and failure without recording credentials or unredacted database URLs.
- `AUDIT.md` records the tested-source Git SHA and is added in a following documentation-only commit; later code/test changes invalidate its results.
- Never add `Co-Authored-By` trailers or use `git commit --author`.

## File Map

Create:

- `tests/e2e/__init__.py`: marks the E2E helper package.
- `tests/e2e/run.py`: unique Compose lifecycle, readiness, scenario dispatch, artifacts, cleanup.
- `tests/e2e/conftest.py`: HTTP client, environment validation, JSONL request recorder.
- `tests/e2e/oracle.py`: independent three-LP graph and brute-force simple-path oracle.
- `tests/e2e/test_docker_api.py`: HTTP-only boot, contract, settlement, provider, and lifecycle phases.
- `tests/e2e/internal_audit.py`: read-only PostgreSQL invariant and scenario-total checks.
- `tests/unit/test_e2e_runner.py`: runner command, cleanup, polling, redaction, and artifact tests.
- `AUDIT.md`: generated only after final evidence execution.

Modify:

- `.gitignore`: ignore `.artifacts/`.
- `docker-compose.yml`: parameterize API host port.
- `pyproject.toml`: exclude E2E from default collection and register scenario markers.
- Existing routing, ledger, idempotency, concurrency, reconciliation, settlement tests for focused gaps.
- `tests/load_test.py`: per-request latency and artifact output.
- `tests/unit/test_load_runner.py`: percentile/statistics/artifact tests.
- `README.md`: exact lane-specific rerun commands and audit workflow.

Production `app/*.py` files change only when a new failing regression proves a defect.

---

### Task 1: Close Focused Unit and PostgreSQL Integration Gaps

**Files:**
- Modify: `tests/unit/test_routing.py`
- Modify: `tests/integration/test_ledger.py`
- Modify: `tests/integration/test_idempotency.py`
- Modify: `tests/integration/test_concurrency.py`
- Modify: `tests/integration/test_reconciliation.py`
- Modify: `tests/integration/test_settlements.py`
- Modify: `tests/conftest.py`
- Modify only on proven defect: corresponding `app/*.py` and migration

**Interfaces:**
- Consumes existing `RateBook`, `SettlementService`, ledger, ORM, and scripted provider APIs.
- Produces additional regression evidence; no new public production API is planned.

- [ ] **Step 1: Add focused routing coverage**

Add tests with these exact behaviors:

- `test_invalid_publication_preserves_last_valid_snapshot`: publish a valid
  USD→PHP quote as version 1; attempt version 2 containing a USD-reachable
  profitable cycle and assert `InvalidRateGraph`; then assert `quote(PHP)` is
  byte-for-byte equal to the original version-1 quote.
- `test_concurrent_readers_only_observe_complete_versions`: alternate complete
  version-1 and version-2 publications while asynchronous readers collect
  `(snapshot_version, hops, aggregate_rate)`; assert every observed tuple is
  exactly the complete v1 or complete v2 tuple and never a mixture.
- `test_disconnected_profitable_cycle_does_not_break_reachable_quote`: combine
  USD→PHP rate 55 with a profitable EUR↔AED cycle unreachable from USD; assert
  `compute_routes(...)[PHP].aggregate_rate == Decimal("55")`.

Run: `.venv/bin/pytest tests/unit/test_routing.py -q`

Expected: existing behavior passes. If a test fails, preserve its RED output and fix only that defect.

- [ ] **Step 2: Add ledger structural and corruption coverage**

Add real-PostgreSQL tests proving:

- A positive but incorrect materialized account balance makes `assert_ledger_invariants()` raise `AssertionError`.
- Application-generated `OPENING`, `RESERVE`, `CONSUME`, and `RELEASE` journals each have exactly two positive, same-currency postings: one debit and one credit.
- PostgreSQL rejects a zero/negative posting amount.
- A mixed-amount concurrent wave whose successful amounts exactly exhaust USD 1,000 has no negative account and rejects the remainder.

Run:

```bash
POSTGRES_HOST_PORT=55432 docker compose up -d db
POSTGRES_HOST_PORT=55432 .venv/bin/pytest tests/integration/test_ledger.py \
  tests/integration/test_concurrency.py -q
```

Expected: all new assertions pass against PostgreSQL. Do not add cross-table triggers unless a required application-generated invariant fails.

- [ ] **Step 3: Add idempotency and reconciliation races**

Add tests with these exact names and outcomes:

- `test_same_key_is_scoped_to_owner`: persist otherwise valid settlements for
  `owner-a` and `owner-b` using the same key; commit both and assert two rows
  exist with distinct IDs.
- `test_concurrent_conflicting_payloads_have_one_winner`: start equal groups
  for amounts 40 and 41 with one owner/key; assert one fingerprint/settlement
  wins, every other-payload call raises `IdempotencyConflict`, and the database
  contains one reserve while the provider records one initiation.
- `test_concurrent_unpaid_reconciliation_releases_once`: reconcile one pending
  settlement from 50 workers returning `UNPAID`; assert every result is
  `FAILED`, one `RELEASE`, no `CONSUME`, and the balance is restored once.
- `test_repeated_unknown_reconciliation_is_a_noop`: perform at least three
  `UNKNOWN` lookups and assert `PENDING_RECONCILIATION`, one reservation, and
  unchanged balances/journals after every call.

Run: `POSTGRES_HOST_PORT=55432 .venv/bin/pytest tests/integration -q`

Expected: all integration tests pass without skipped correctness cases.

- [ ] **Step 4: Cover unexpected provider failures safely**

Use test providers that raise `RuntimeError` from `initiate()` or `lookup()`.
Assert initiation failure propagates while leaving the settlement
`PAYOUT_IN_PROGRESS` with funds reserved; lookup failure propagates while
leaving `PENDING_RECONCILIATION` and its reserve unchanged. These are
recoverable persisted states, not converted into false success/failure.

Run: `POSTGRES_HOST_PORT=55432 .venv/bin/pytest tests/integration/test_settlements.py tests/integration/test_reconciliation.py -q`

Expected: tests pass against current safe behavior. A failure requires its own RED/GREEN production fix.

- [ ] **Step 5: Verify and commit focused coverage**

```bash
.venv/bin/pytest tests/unit -q
POSTGRES_HOST_PORT=55432 .venv/bin/pytest tests/integration -q
.venv/bin/python -m compileall -q app tests
git diff --check
git add app alembic tests
git commit -m "test: expand settlement edge-case coverage"
```

---

### Task 2: Build the Isolated Docker E2E Runner

**Files:**
- Create: `tests/e2e/__init__.py`
- Create: `tests/e2e/run.py`
- Create: `tests/unit/test_e2e_runner.py`
- Modify: `docker-compose.yml`
- Modify: `.gitignore`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces `RunConfig`, `ScenarioSpec`, `compose_command()`, `wait_for_health()`, `capture_artifacts()`, `run_scenario()`, and `main()`.
- Later tasks consume the scenario environment variables and artifact directory.

- [ ] **Step 1: Write failing runner contract tests**

Lock these shapes:

```python
@dataclass(frozen=True)
class RunConfig:
    run_id: str
    project_name: str
    api_host_port: int
    postgres_host_port: int
    artifact_dir: Path
    payout_mode: str


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    payout_mode: str
    pytest_marker: str | None
    needs_internal_audit: bool = False
    is_load: bool = False


def compose_command(config: RunConfig, *args: str) -> list[str]:
    return ["docker", "compose", "-p", config.project_name, *args]
```

Tests must initially fail because `tests.e2e.run` does not exist, then prove:

- Every Compose command includes the exact unique `-p` project.
- Environment carries both selected host ports and payout mode.
- Health polling checks exact status/body and times out without declaring success.
- Scenario failure remains the process exit code even if teardown also fails.
- `down -v --remove-orphans` always executes in `finally`.
- Artifact capture is attempted before teardown on both success and failure.
- Compose config redaction removes passwords and password-bearing URLs.

Run: `.venv/bin/pytest tests/unit/test_e2e_runner.py -q`

Expected RED: `ModuleNotFoundError: tests.e2e`.

- [ ] **Step 2: Implement lifecycle and artifact primitives**

`run.py` must use `subprocess.run(..., capture_output=True, text=True)` through
one wrapper, `time.monotonic()` for deadlines, and a socket bound to port zero
for candidate port selection. Retry `up` only when stderr proves a port bind
collision.

Always create:

```text
.artifacts/e2e/<run-id>/
  metadata.txt
  compose-config.yml
  compose-ps.txt
  images.txt
  readiness.log
  scenario.stdout
  scenario.stderr
  api.log
  db.log
  teardown.txt
```

Metadata includes tested SHA/dirty state, UTC times, host/Python/Docker/Compose,
project, ports, payout mode, container/image/volume IDs. Never dump the full
process environment.

- [ ] **Step 3: Isolate Compose and test collection**

Modify API port mapping:

```yaml
ports:
  - "${API_HOST_PORT:-8000}:8000"
```

Add `.artifacts/` to `.gitignore`. Set default pytest collection to unit and
integration only:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests/unit", "tests/integration"]
markers = [
  "boot_contract: Docker boot and public contract",
  "settlement_idempotency: Docker quote and replay behavior",
  "provider_reconciliation: Docker provider outcome behavior",
  "concurrency_funds: Docker overspend behavior",
  "lifecycle_recovery: Docker restart and outage behavior",
]
```

E2E files run only through explicit paths from the lifecycle runner; default
pytest must not silently skip or accidentally execute Docker correctness tests.

- [ ] **Step 4: Verify runner and commit**

```bash
.venv/bin/pytest tests/unit/test_e2e_runner.py -q
.venv/bin/pytest tests/unit -q
docker compose config --quiet
git diff --check
git add .gitignore docker-compose.yml pyproject.toml tests/e2e tests/unit/test_e2e_runner.py
git commit -m "test: add isolated Docker E2E runner"
```

---

### Task 3: Add HTTP-Only Contract, Settlement, and Provider Scenarios

**Files:**
- Create: `tests/e2e/conftest.py`
- Create: `tests/e2e/oracle.py`
- Create: `tests/e2e/test_docker_api.py`
- Modify: `tests/e2e/run.py`

**Interfaces:**
- Consumes `E2E_BASE_URL`, `E2E_SCENARIO`, and `E2E_ARTIFACT_DIR` from runner.
- Produces JSONL request evidence and independent `OracleQuote` results.

- [ ] **Step 1: Implement the independent oracle under unit tests**

Define `OracleHop(source: str, target: str, lp: str, rate: Decimal)` and
`OracleQuote(aggregate_rate: Decimal, hops: tuple[OracleHop, ...])` as frozen
dataclasses. Expose `generate_oracle_edges(version: int) ->
tuple[OracleHop, ...]` and `best_simple_path(version: int, target: str) ->
OracleQuote` without any `app.*` import.

Replicate the documented five anchors, coefficients, and LP factors, then
enumerate every simple USD-to-target path. Select maximum product and the same
lexical `(source, target, lp)` tie break. Add oracle unit cases with known
direct/multihop results before using it in Docker tests.

- [ ] **Step 2: Add boot and validation black-box tests**

`test_docker_api.py` may import stdlib, pytest, HTTPX, and test-local helpers
only. Assert:

- Exact health 200 body.
- `/docs`, `/redoc`, `/openapi.json`, `/`, and `/unknown` return 404.
- Missing each required header and both headers return 422 with expected `loc`/`type` shape.
- Empty headers, malformed JSON, empty object, zero/negative/overprecision/
  oversized amounts, GBP, and malformed UUID return 422.
- Maximum accepted amount returns exact 409 insufficient funds.
- A valid absent UUID returns exact 404 for GET and reconciliation.

Do not claim black-box enumeration of every possible route; the internal API
test already proves the exact four-route registration.

- [ ] **Step 3: Add settlement, replay, concurrency, and quote tests**

On a fresh load-mode project:

- Create USD 100 to PHP; validate UUID, exact response keys, positive rates,
  contiguous route, aggregate product and quoted output quantized to `1E-8`.
- Compare route against `best_simple_path(snapshot_version, target)`.
- Replay `100.0` as `100.00`; assert original ID/quote.
- Changed amount and target each return exact 409.
- Send 100 concurrent same-key requests; assert one UUID and one final GET
  result. Responses may transiently expose `PAYOUT_IN_PROGRESS`, but no second
  settlement may appear.
- Send a 75ms-paced multi-target wave; require multiple versions and validate
  each with the independent oracle.

Write one JSONL record per HTTP attempt containing scenario, phase, index,
method/path template, UTC start, monotonic latency, HTTP status, settlement ID,
settlement status, snapshot version, and sanitized error.

- [ ] **Step 4: Add deterministic provider/reconciliation tests**

On a fresh default-mode project, submit 20 unique requests concurrently:

- Exact 14 success, 3 failed, 3 pending.
- Whole wave under 15 seconds so three five-second timeouts overlap.
- Reconcile terminal rows twice; response unchanged.
- Reconcile pending rows twice; remain pending, same quote/route/ID, and each
  lookup is short rather than a second five-second initiation.

- [ ] **Step 5: Run Docker groups and commit**

```bash
.venv/bin/python tests/e2e/run.py --scenario boot-contract
.venv/bin/python tests/e2e/run.py --scenario settlement-idempotency
.venv/bin/python tests/e2e/run.py --scenario provider-reconciliation
.venv/bin/pytest tests/unit -q
git diff --check
git add tests/e2e
git commit -m "test: add Docker API black-box scenarios"
```

Expected: each scenario uses a different Compose project/volume, passes, saves
artifacts, and leaves no project containers or volumes behind.

---

### Task 4: Add Funds, Lifecycle, Recovery, and SQL Audit Scenarios

**Files:**
- Create: `tests/e2e/internal_audit.py`
- Modify: `tests/e2e/test_docker_api.py`
- Modify: `tests/e2e/run.py`

**Interfaces:**
- Produces `AuditAssertion`/`AuditResult` and scenario-specific audit functions.
- Consumes isolated database URL and never writes to PostgreSQL.

- [ ] **Step 1: Write audit helper tests and implementation**

```python
@dataclass(frozen=True)
class AuditAssertion:
    label: str
    expected: object
    actual: object
    passed: bool


@dataclass(frozen=True)
class AuditResult:
    assertions: tuple[AuditAssertion, ...]

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.assertions)
```

Every audit opens `BEGIN TRANSACTION READ ONLY`, prints label/expected/actual,
and fails nonzero on mismatch. Shared queries prove zero negative accounts,
balanced journal/currency groups, exactly two postings with one debit/credit,
positive USD postings, no duplicate owner/key or settlement/event, provider ID
equals settlement ID, posting-derived balances equal materialized balances, and
status/event consistency.

- [ ] **Step 2: Add exact-boundary funds scenario**

On a fresh load project send 101 concurrent unique USD 1,000 requests. Assert
100 HTTP 200 success and one exact 409 insufficient funds. SQL audit expects
100 settlements/reserves/consumes, zero releases, customer available/reserved
zero, system USD Omnibus zero, and every shared invariant passing.

- [ ] **Step 3: Add lifecycle phase orchestration**

Runner-owned ordered phases use an artifact `lifecycle-state.json`:

1. Create/record one terminal settlement and database counts.
2. Restart API, poll health, GET/replay terminal row, and audit unchanged effects.
3. Create a fresh 20-operation default wave after restart; record a pending row.
4. Restart API again; GET/replay/reconcile pending remains identical and reserved.
5. Stop DB; poll exact `503 {"detail": "database unavailable"}`.
6. Start DB; poll exact `200 {"status": "ok"}`.

Capture logs after each restart/outage phase. Record provider in-memory state
loss as a known safe-but-unresolved limitation, not as a passing definitive
reconciliation claim.

- [ ] **Step 4: Run scenarios and commit**

```bash
.venv/bin/python tests/e2e/run.py --scenario concurrency-funds
.venv/bin/python tests/e2e/run.py --scenario lifecycle-recovery
.venv/bin/pytest tests/unit -q
POSTGRES_HOST_PORT=55432 .venv/bin/pytest tests/integration -q
git diff --check
git add tests/e2e
git commit -m "test: cover Docker funds and recovery lifecycle"
```

---

### Task 5: Extend Canonical Load Metrics and Artifacts

**Files:**
- Modify: `tests/load_test.py`
- Modify: `tests/unit/test_load_runner.py`
- Modify: `tests/e2e/run.py`

**Interfaces:**
- Produces `RequestResult`, `LatencySummary`, `nearest_rank()`, and artifact output.
- Preserves existing fixed accounting and route audit.

- [ ] **Step 1: Write failing statistics and artifact tests**

```python
@dataclass(frozen=True)
class RequestResult:
    request_id: str
    index: int
    latency_ms: float
    http_status: int | None
    settlement_id: str | None
    settlement_status: str | None
    snapshot_version: int | None
    error: str | None


@dataclass(frozen=True)
class LatencySummary:
    count: int
    success_count: int
    error_count: int
    wall_seconds: float
    throughput_requests_per_second: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
```

Test nearest-rank percentile behavior for empty (error), singleton, even, and
1,000-value samples; throughput uses wave wall time, not summed latency. Test
JSONL contains no headers, credentials, or DB URL. Preserve the fixed proof
argument rejection.

Expected RED: statistics/artifact helpers do not exist.

- [ ] **Step 2: Implement per-request timing without weakening correctness**

Use `time.perf_counter()` around every request and the entire wave. Preserve
immediate failure on transport/non-200 response and all existing read-only
accounting/routing assertions. Add optional `--artifact-dir`; when supplied,
write `requests.jsonl`, `latency.json`, and `internal-audit.txt` atomically.

- [ ] **Step 3: Integrate canonical load into isolated runner**

Fresh load-mode project command:

```bash
.venv/bin/python tests/load_test.py \
  --base-url http://127.0.0.1:<api-port> \
  --database-url postgresql://netaro:netaro@127.0.0.1:<db-port>/netaro \
  --requests 1000 --concurrency 1000 --amount 100 \
  --artifact-dir .artifacts/e2e/<run-id>
```

Expected exact 700/150/150 accounting PASS plus count, throughput, p50, p95,
p99, maximum, multiple snapshots, and zero transport errors.

- [ ] **Step 4: Verify and commit load evidence support**

```bash
.venv/bin/pytest tests/unit/test_load_runner.py -q
.venv/bin/python tests/e2e/run.py --scenario canonical-load
git diff --check
git add tests/load_test.py tests/unit/test_load_runner.py tests/e2e/run.py
git commit -m "test: capture canonical load evidence"
```

---

### Task 6: Run All Evidence and Publish AUDIT.md

**Files:**
- Modify before evidence commit: `README.md`
- Create after evidence run: `AUDIT.md`

**Interfaces:**
- Consumes artifacts and summaries from Tasks 1-5.
- Produces a committed, readable audit snapshot plus exact rerun commands.

- [ ] **Step 1: Document lane-specific rerun commands**

README must contain:

```bash
.venv/bin/pytest tests/unit -q
POSTGRES_HOST_PORT=55432 docker compose up -d db
POSTGRES_HOST_PORT=55432 .venv/bin/pytest tests/integration -q
.venv/bin/python tests/e2e/run.py --scenario all
```

Explain unique project isolation, ignored artifact paths, black-box versus SQL
evidence, destructive cleanup limited to runner-owned projects, and tested-SHA
semantics.

- [ ] **Step 2: Commit the tested source tree**

```bash
.venv/bin/pytest tests/unit -q
POSTGRES_HOST_PORT=55432 .venv/bin/pytest tests/integration -q
git diff --check
git add README.md tests docker-compose.yml pyproject.toml .gitignore app alembic
git commit -m "docs: document complete verification workflow"
git status --short
git rev-parse HEAD
```

Record this clean commit as `TESTED_SOURCE_SHA`.

- [ ] **Step 3: Execute the final evidence run**

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest tests/unit -q
POSTGRES_HOST_PORT=55432 docker compose up -d db
POSTGRES_HOST_PORT=55432 .venv/bin/pytest tests/integration -q
.venv/bin/python tests/e2e/run.py --scenario all
.venv/bin/python -m compileall -q app tests alembic
git diff --check
docker build .
```

Expected: zero unit/integration failures or skipped correctness cases; all six
isolated E2E/load projects pass and tear down; artifacts exist for each group;
package install and image build succeed.

- [ ] **Step 4: Create AUDIT.md from actual evidence**

Include:

```text
| Lane | Passed | Failed | Skipped | Duration | Evidence |

| ID | Lane | Requirement | Scenario | Expected | Actual | Status | Duration | Evidence | Notes |

| Metric | Expected | Actual | Status | Evidence |
```

Record tested source SHA, dirty state, UTC start/end, platform, Python,
PostgreSQL, Docker/Compose, image IDs, total duration, artifact root, every
scenario result, exact load/accounting values, latency percentiles, known
limitations, and exact rerun commands. Explicitly state that `AUDIT.md` is the
following documentation-only commit and that code/test changes require rerun.

- [ ] **Step 5: Validate and commit the audit document**

```bash
rg -n "TBD|TODO|FIXME|placeholder" AUDIT.md && exit 1 || true
git diff --check
git add AUDIT.md
git commit -m "docs: add verification audit"
git status --short
```

## Final Review Checklist

- [ ] Existing and added unit tests pass.
- [ ] PostgreSQL integration tests pass with no skipped correctness cases.
- [ ] Every E2E scenario starts from a unique project/volume and tears down only itself.
- [ ] Black-box test files contain no production or database imports.
- [ ] Boot, validation, quote, replay, provider mix, funds, restart, DB outage, and recovery scenarios pass.
- [ ] Internal SQL audits are read-only and clearly labelled.
- [ ] Canonical load produces exact accounting/routing totals and latency metrics.
- [ ] Failure paths preserve logs/artifacts and nonzero exit status.
- [ ] README rerun commands work from `main`.
- [ ] `AUDIT.md` contains actual evidence, tested SHA, known limitations, and rerun commands.
- [ ] Package installation, Alembic/schema verification, Docker build, compile, and diff checks pass.
