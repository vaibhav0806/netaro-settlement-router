# Netaro Settlement Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Docker Compose-runnable FastAPI/PostgreSQL settlement service that maximizes receiver FX output and preserves double-entry and idempotency invariants under 1,000 concurrent payouts.

**Architecture:** A process-local rate book atomically publishes immutable snapshots with precomputed max-product Bellman-Ford routes from USD. A settlement service uses short PostgreSQL transactions and ordered `SELECT FOR UPDATE` locks to reserve and finalize USD postings, while the payout call occurs outside all locks. Provider timeouts retain the reserve in `PENDING_RECONCILIATION` and are resolved only through an idempotent status lookup.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2 async ORM, asyncpg, Alembic, PostgreSQL 16, pytest, pytest-asyncio, HTTPX, Docker Compose.

## Global Constraints

- The complete recorded AI-assisted build session must not exceed four hours.
- The application must start locally with `docker-compose up`/`docker compose up`.
- Use PostgreSQL—not SQLite—for ledger, locking, idempotency, and concurrency tests.
- Use `Decimal`/PostgreSQL `NUMERIC`; never use binary floating point for money or rates.
- Implement max-product Bellman-Ford in `O(VE)` per immutable snapshot; document the accepted deviation from the requested `O(V+E)`.
- Never hold a database transaction, connection, or row lock while awaiting the payout provider.
- Never automatically retry a timed-out payout; retain its reserve and reconcile by provider operation ID.
- Use only three externally visible outcome states: `SUCCESS`, `FAILED`, and `PENDING_RECONCILIATION`; persist `RESERVED` and `PAYOUT_IN_PROGRESS` for recovery.
- Ignore fees, capacity, slippage, and quote expiry. Post only USD accounting entries while seeding both required Omnibus USD and Omnibus USDC accounts.
- Do not add authentication, a message queue, an outbox, distributed locks, or multi-currency valuation accounting to the four-hour implementation.
- Do not add `Co-Authored-By` trailers or use `git commit --author`.

## File Map

- `pyproject.toml`: package metadata, runtime dependencies, pytest configuration.
- `.env.example`: database, demo seed, and mock-provider settings.
- `Dockerfile`, `docker-compose.yml`: API/PostgreSQL build, health, migration, and startup.
- `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial.py`: async migration configuration and complete schema.
- `app/db.py`: settings, async engine/session factory, database dependency/readiness check.
- `app/seed.py`: idempotent balanced demo-account opening entry.
- `app/models.py`: ORM enums and `Account`, `Settlement`, `JournalTransaction`, and `Posting`.
- `app/schemas.py`: API commands/responses and canonical request fingerprint.
- `app/routing.py`: rate graph types, Bellman-Ford, immutable `RateBook`, 50 ms simulator.
- `app/ledger.py`: locked `reserve`, `consume`, and `release` operations; invariant audit.
- `app/provider.py`: provider protocol and deterministic/mock 70/15/15 implementation.
- `app/service.py`: idempotency, transaction boundaries, state transitions, reconciliation.
- `app/main.py`: lifespan, dependency wiring, endpoints, and exception mapping.
- `tests/conftest.py`: real PostgreSQL reset/seed, sessions, app client, rate/provider fakes.
- `tests/unit/test_routing.py`: routing and immutable-snapshot tests.
- `tests/integration/test_ledger.py`: double-entry and locked balance tests.
- `tests/integration/test_settlements.py`: payout outcome/state tests.
- `tests/integration/test_idempotency.py`: sequential and concurrent replay tests.
- `tests/integration/test_concurrency.py`: overspend and competing-finalizer tests.
- `tests/integration/test_reconciliation.py`: timeout/reconciliation tests.
- `tests/integration/test_api.py`: endpoint and health contracts.
- `tests/load_test.py`: 1,000-request runner and post-run accounting audit.
- `README.md`: run, test, load-test, API, assumptions, and recording instructions.

---

### Task 1: Project Foundation and Deterministic FX Routing

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `app/__init__.py`
- Create: `app/routing.py`
- Create: `tests/unit/test_routing.py`

**Interfaces:**
- Produces: `Currency`, `Edge`, `RouteHop`, `RouteQuote`, `RateSnapshot`, `RateBook`, `compute_routes()`.
- Consumes: no application code; this task is pure and database-independent.

- [ ] **Step 1: Initialize project metadata and the test runner**

If the workspace still has no Git metadata, run `git init` once. Create `pyproject.toml` with these dependency groups and pytest settings:

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "netaro-router"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "alembic>=1.13,<2",
  "asyncpg>=0.29,<1",
  "fastapi>=0.115,<1",
  "pydantic-settings>=2.5,<3",
  "sqlalchemy[asyncio]>=2.0,<3",
  "uvicorn[standard]>=0.30,<1",
]

[project.optional-dependencies]
dev = [
  "httpx>=0.27,<1",
  "pytest>=8,<10",
  "pytest-asyncio>=0.24,<2",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

Create `.env.example`:

```dotenv
DATABASE_URL=postgresql+asyncpg://netaro:netaro@localhost:5432/netaro
DEMO_OWNER_ID=demo-customer
DEMO_BALANCE_USD=100000.00
PAYOUT_TIMEOUT_SECONDS=5
```

Run: `python -m pip install -e ".[dev]"`

Expected: editable installation succeeds.

- [ ] **Step 2: Write failing routing tests**

Define tests using exact `Decimal` values:

```python
def test_selects_path_with_greatest_receiver_output():
    edges = (
        Edge(Currency.USD, Currency.PHP, "direct", Decimal("55")),
        Edge(Currency.USD, Currency.EUR, "eur-lp", Decimal("0.92")),
        Edge(Currency.EUR, Currency.PHP, "php-lp", Decimal("61")),
    )
    quote = compute_routes(edges, version=7)[Currency.PHP]
    assert quote.aggregate_rate == Decimal("56.12")
    assert tuple(hop.target for hop in quote.hops) == (Currency.EUR, Currency.PHP)
    assert Decimal("100") * quote.aggregate_rate == Decimal("5612.00")


def test_profitable_cycle_invalidates_snapshot():
    edges = (
        Edge(Currency.USD, Currency.EUR, "a", Decimal("0.9")),
        Edge(Currency.EUR, Currency.USD, "b", Decimal("1.2")),
    )
    with pytest.raises(InvalidRateGraph):
        compute_routes(edges, version=1)
```

Add explicit cases for the best of three parallel LP edges, equal-product tie stability under reversed input order, zero/negative rates, disconnected PHP, and `RateBook` returning all fields from one snapshot version.

Run: `pytest tests/unit/test_routing.py -q`

Expected: collection fails because `app.routing` does not exist.

- [ ] **Step 3: Implement the routing interfaces and max-product relaxation**

Create these exact public shapes in `app/routing.py`:

```python
class Currency(StrEnum):
    USD = "USD"
    USDC = "USDC"
    EUR = "EUR"
    PHP = "PHP"
    AED = "AED"


@dataclass(frozen=True)
class Edge:
    source: Currency
    target: Currency
    lp: str
    rate: Decimal


@dataclass(frozen=True)
class RouteHop:
    source: Currency
    target: Currency
    lp: str
    rate: Decimal


@dataclass(frozen=True)
class RouteQuote:
    snapshot_version: int
    target: Currency
    aggregate_rate: Decimal
    hops: tuple[RouteHop, ...]


@dataclass(frozen=True)
class RateSnapshot:
    version: int
    routes: Mapping[Currency, RouteQuote]


class InvalidRateGraph(ValueError):
    pass


class RouteNotFound(LookupError):
    pass


def compute_routes(
    edges: tuple[Edge, ...],
    version: int,
    source: Currency = Currency.USD,
) -> Mapping[Currency, RouteQuote]:
    """Return maximum-product routes, rejecting invalid/profitable cycles."""
```

Implementation rules:

1. Reject every `rate <= 0`.
2. Sort edges by `(source.value, target.value, lp)` and collapse parallel edges to the greatest rate; use lexicographically smallest LP on equal rates.
3. Store each candidate as `(aggregate_rate, tuple[RouteHop, ...])`.
4. Perform `V - 1` copied relaxation rounds (`next_best = best.copy()`) so results are edge-order independent; stop early when unchanged.
5. On equal products, select the lexicographically smallest tuple of `(source, target, lp)`.
6. Perform one additional relaxation and raise `InvalidRateGraph` if any USD-reachable candidate strictly improves.

`RateBook.publish(edges)` computes a complete snapshot before replacing its single snapshot reference. `RateBook.quote(target)` returns the stored quote or raises `RouteNotFound`. Its simulator derives cross-rates for exactly `LP_A`, `LP_B`, and `LP_C` from currency anchor values and positive LP spreads, then publishes every `0.05` seconds so it does not accidentally generate profitable cycles.

- [ ] **Step 4: Run and extend routing tests**

Run: `pytest tests/unit/test_routing.py -q`

Expected: all routing tests pass, including reversed-edge-order tie tests and snapshot-version consistency.

- [ ] **Step 5: Commit the independently testable routing slice**

```bash
git add pyproject.toml .env.example app tests/unit
git commit -m "feat: add deterministic FX routing"
```

---

### Task 2: PostgreSQL Schema and Locked Double-Entry Ledger

**Files:**
- Create: `app/db.py`
- Create: `app/models.py`
- Create: `app/ledger.py`
- Create: `app/seed.py`
- Create: `docker-compose.yml` with the PostgreSQL service
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_initial.py`
- Create: `tests/conftest.py`
- Create: `tests/integration/test_ledger.py`

**Interfaces:**
- Consumes: `Currency` from `app.routing`.
- Produces: `SessionFactory`, ORM models/enums, `reserve()`, `consume()`, `release()`, and `assert_ledger_invariants()`.

- [ ] **Step 1: Define schema contracts before generating the migration**

Create enums with these values:

```python
class AccountClass(StrEnum): ASSET = "ASSET"; LIABILITY = "LIABILITY"
class AccountPurpose(StrEnum): AVAILABLE = "AVAILABLE"; RESERVED = "RESERVED"; OMNIBUS = "OMNIBUS"
class PostingSide(StrEnum): DEBIT = "DEBIT"; CREDIT = "CREDIT"
class JournalEvent(StrEnum): OPENING = "OPENING"; RESERVE = "RESERVE"; CONSUME = "CONSUME"; RELEASE = "RELEASE"
class SettlementStatus(StrEnum):
    RESERVED = "RESERVED"
    PAYOUT_IN_PROGRESS = "PAYOUT_IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"
```

Create SQLAlchemy models with UUID primary keys and `NUMERIC(24, 8)` monetary/rate columns:

- `accounts`: non-null `owner_id` (use `system` for omnibus accounts), `currency`, `account_class`, `purpose`, `balance`; unique `(owner_id, currency, purpose)`; `balance >= 0`.
- `settlements`: owner/key/fingerprint, USD amount, target, JSON route, snapshot version, aggregate rate, quoted amount, provider ID, status/timestamps; unique `(owner_id, idempotency_key)`; positive amount/rate/quote checks.
- `journal_transactions`: nullable settlement ID and event; unique `(settlement_id, event)` for non-opening events.
- `postings`: journal/account/currency/side/amount; `amount > 0`.

Define `SessionFactory = async_sessionmaker(engine, expire_on_commit=False)` and make `get_session()` yield and close one session without implicitly committing it.

- [ ] **Step 2: Write failing PostgreSQL ledger tests**

Create the PostgreSQL 16 service in `docker-compose.yml` now so integration tests can run before the API container exists. It must expose port 5432, use the `netaro` database/user/password, and include a `pg_isready` health check.

`tests/conftest.py` must point to a dedicated PostgreSQL database, create/drop schema once per test session, truncate tables between integration tests, and seed balances through this balanced opening entry:

```text
Debit  Omnibus USD asset                 1000
Credit Customer Available USD liability  1000
```

Write these exact assertions:

```python
async def test_reserve_is_balanced_and_moves_available_to_reserved(seeded_accounts, session):
    settlement = await make_settlement(session, amount=Decimal("40"))
    await reserve(session, settlement)
    await session.commit()
    assert await balance(session, "AVAILABLE") == Decimal("960")
    assert await balance(session, "RESERVED") == Decimal("40")
    await assert_ledger_invariants(session)


async def test_two_concurrent_spends_cannot_overspend(session_factory, seeded_accounts):
    results = await asyncio.gather(
        reserve_in_new_session(session_factory, "a", Decimal("700")),
        reserve_in_new_session(session_factory, "b", Decimal("700")),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, InsufficientFunds) for result in results) == 1
    assert await available_balance(session_factory) == Decimal("300")
```

Also test insufficient funds writes no journal, consume credits Omnibus USD and clears reserved, release restores available, duplicate settlement/event is rejected, and every journal balances per currency.

Run: `docker compose up -d db && pytest tests/integration/test_ledger.py -q`

Expected: fail because the schema and ledger operations are incomplete.

- [ ] **Step 3: Implement account-class-aware postings under ordered locks**

Expose this API from `app/ledger.py`; callers own commit/rollback:

```python
class InsufficientFunds(Exception):
    pass


async def reserve(session: AsyncSession, settlement: Settlement) -> None: ...
async def consume(session: AsyncSession, settlement: Settlement) -> None: ...
async def release(session: AsyncSession, settlement: Settlement) -> None: ...
async def assert_ledger_invariants(session: AsyncSession) -> None: ...
```

For each operation, fetch affected accounts ordered by `Account.id` using `with_for_update()`. Recheck the debited account's available normal-side balance, create one journal with exactly two positive USD postings, and update balances in the same transaction. Apply postings as follows:

```python
def apply_posting(account: Account, side: PostingSide, amount: Decimal) -> None:
    increases = (
        account.account_class == AccountClass.ASSET and side == PostingSide.DEBIT
    ) or (
        account.account_class == AccountClass.LIABILITY and side == PostingSide.CREDIT
    )
    account.balance += amount if increases else -amount
    if account.balance < 0:
        raise InsufficientFunds
```

`assert_ledger_invariants()` raises `AssertionError` when any account is negative, any journal/currency debit sum differs from its credit sum, or a materialized balance differs from opening balance plus normal-side posting movement.

Create `seed_demo_accounts(session, owner_id, amount)` in `app/seed.py`. It inserts Customer Available/Reserved USD plus system Omnibus USD/USDC and one balanced USD opening journal. Repeated execution must detect the existing owner/account keys and make no balance or journal change. Expose `python -m app.seed` as the container startup command.

- [ ] **Step 4: Create and apply the Alembic migration**

Configure Alembic to import `Base.metadata`. The initial migration must create all enum types, tables, foreign keys, numeric checks, unique owner/idempotency and owner/currency/purpose constraints, and a partial unique index for `(settlement_id, event)` where settlement ID is non-null.

Run:

```bash
alembic upgrade head
pytest tests/integration/test_ledger.py -q
```

Expected: migration succeeds and all ledger tests pass against PostgreSQL.

- [ ] **Step 5: Commit the locked ledger slice**

```bash
git add app/db.py app/models.py app/ledger.py app/seed.py docker-compose.yml alembic.ini alembic tests
git commit -m "feat: add locked double-entry ledger"
```

---

### Task 3: Idempotent Settlement State Machine and Payout Reconciliation

**Files:**
- Create: `app/schemas.py`
- Create: `app/provider.py`
- Create: `app/service.py`
- Create: `tests/integration/test_settlements.py`
- Create: `tests/integration/test_idempotency.py`
- Create: `tests/integration/test_concurrency.py`
- Create: `tests/integration/test_reconciliation.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: routing quotes, ORM/session factory, and ledger operations from Tasks 1–2.
- Produces: request/response models, provider protocol/mock, and `SettlementService`.

- [ ] **Step 1: Define commands, fingerprints, and provider contract**

```python
class SettlementCreate(BaseModel):
    amount_usd: Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=8)]
    target_currency: Currency


def request_fingerprint(owner_id: str, command: SettlementCreate) -> str:
    normalized = f"{owner_id}|{command.amount_usd.normalize()}|{command.target_currency.value}"
    return hashlib.sha256(normalized.encode()).hexdigest()


class ProviderResult(StrEnum): PAID = "PAID"; UNPAID = "UNPAID"
class ProviderLookup(StrEnum): PAID = "PAID"; UNPAID = "UNPAID"; UNKNOWN = "UNKNOWN"; NOT_FOUND = "NOT_FOUND"
class PayoutTimeout(TimeoutError): pass


class PayoutProvider(Protocol):
    async def initiate(
        self, settlement_id: UUID, amount_usd: Decimal,
        target_currency: Currency, quoted_amount: Decimal,
    ) -> ProviderResult: ...
    async def lookup(self, settlement_id: UUID) -> ProviderLookup: ...
```

`SettlementRead` contains settlement ID, status, USD amount, target, quote, aggregate rate, snapshot version, and route hops. Its ORM conversion must quantize only at the API currency boundary, not during path comparison.

- [ ] **Step 2: Write failing state, idempotency, and reconciliation tests**

Implement a concurrency-safe `ScriptedPayoutProvider` fixture with explicit initial result and lookup sequence. Add these tests:

- Success: USD 40 from available USD 100 leaves available 60, reserved 0, one `RESERVE`, one `CONSUME`, status `SUCCESS`.
- `503`/unpaid: available returns to 100, reserved 0, one `RELEASE`, status `FAILED`.
- Timeout: available 60, reserved 40, no terminal journal, exactly one initiation, status `PENDING_RECONCILIATION`.
- Same owner/key/payload: same settlement/quote, one reserve, one provider initiation.
- Same owner/key with changed amount or target: `IdempotencyConflict`, original unchanged.
- 100 simultaneous same-key calls: one settlement, one reserve, one provider initiation.
- 100 unique USD 20 requests against USD 1,000 while provider is paused: exactly 50 reservations and 50 `InsufficientFunds`; available 0, reserved 1,000.
- 50 competing success finalizers: exactly one `CONSUME`.
- Unknown reconciliation: remains pending and preserves the reserve.
- Paid/unpaid reconciliation: consumes/releases exactly once; repeat is a no-op.
- Recovery from `PAYOUT_IN_PROGRESS`: lookup happens first; only `NOT_FOUND` may initiate with the same ID.

Run: `pytest tests/integration -q`

Expected: new tests fail because `SettlementService` is absent.

- [ ] **Step 3: Implement the mock provider without random test behavior**

`MockPayoutProvider.initiate()` must deduplicate by settlement UUID. Default demo behavior assigns 70/15/15 outcomes; injected tests use explicit scripts. For timeout, store a stable internal `PAID`, `UNPAID`, or `UNKNOWN` operation and raise `PayoutTimeout` after the configured delay. A repeated initiation returns the stored definitive result or raises the same timeout without creating a second operation. `lookup()` never initiates.

- [ ] **Step 4: Implement settlement transaction boundaries**

Expose:

```python
class IdempotencyConflict(Exception): pass
class SettlementNotFound(Exception): pass


class SettlementService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        rates: RateBook,
        provider: PayoutProvider,
    ) -> None: ...

    async def create(
        self, owner_id: str, idempotency_key: str, command: SettlementCreate,
    ) -> SettlementRead: ...

    async def get(self, settlement_id: UUID) -> SettlementRead: ...
    async def reconcile(self, settlement_id: UUID) -> SettlementRead: ...
```

`create()` follows this exact sequence:

1. Query existing owner/key; compare fingerprint and return immediately on replay.
2. Capture one `RateBook` quote and build the complete route JSON.
3. In a short transaction, insert settlement and call `reserve()`. On unique violation, rollback, reload the winner, compare fingerprint, and return without invoking payout.
4. In a second transaction, lock the settlement and conditionally change `RESERVED` to `PAYOUT_IN_PROGRESS`; only the winner continues.
5. Close the transaction/session before awaiting `provider.initiate()`.
6. In a third transaction, lock the settlement, recheck `PAYOUT_IN_PROGRESS`, then consume/`SUCCESS`, release/`FAILED`, or retain/`PENDING_RECONCILIATION`.

`reconcile()` performs provider lookup outside a transaction, then locks and rechecks the settlement before exactly one consume/release. `UNKNOWN` preserves pending state. For crash-left `PAYOUT_IN_PROGRESS`, query first; `PAID`/`UNPAID` finalize, `UNKNOWN` remains unchanged, and only definitive `NOT_FOUND` permits one idempotent initiation.

- [ ] **Step 5: Run concurrency tests repeatedly**

Run:

```bash
pytest tests/integration/test_idempotency.py tests/integration/test_concurrency.py -q
pytest tests/integration/test_idempotency.py tests/integration/test_concurrency.py -q
pytest tests/integration/test_reconciliation.py tests/integration/test_settlements.py -q
```

Expected: all runs pass with no escaped unique violations, deadlocks, duplicate journals, or balance drift.

- [ ] **Step 6: Commit the state-machine slice**

```bash
git add app/schemas.py app/provider.py app/service.py tests
git commit -m "feat: add settlement state machine"
```

---

### Task 4: FastAPI Contracts, Health Check, and Docker Startup

**Files:**
- Create: `app/main.py`
- Create: `Dockerfile`
- Modify: `docker-compose.yml` to add the API service
- Create: `tests/integration/test_api.py`
- Modify: `app/db.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `SettlementService` and `RateBook`.
- Produces: runnable ASGI application and the four approved endpoints.

- [ ] **Step 1: Write failing endpoint-contract tests**

Use `httpx.AsyncClient` with injected rate/provider/session dependencies. Test:

```python
async def test_health_checks_postgres(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_create_requires_idempotency_and_owner_headers(client):
    response = await client.post(
        "/settlements", json={"amount_usd": "40", "target_currency": "PHP"}
    )
    assert response.status_code == 422
```

Also assert POST creates/replays, GET returns quote/status, reconciliation is idempotent, changed payload maps to `409`, missing settlement maps to `404`, insufficient funds maps to `409`, no route maps to `422`, and failed database readiness maps to `503`.

Run: `pytest tests/integration/test_api.py -q`

Expected: fail because `app.main` does not exist.

- [ ] **Step 2: Implement database readiness and API wiring**

`check_database()` executes `SELECT 1`. Implement:

```text
POST /settlements
  headers: Idempotency-Key, X-Owner-ID
  body: SettlementCreate

GET /settlements/{settlement_id}
POST /settlements/{settlement_id}/reconcile
GET /health
```

The FastAPI lifespan starts `RateBook` only after publishing its initial snapshot and stops its simulator task cleanly. Exception handlers return stable JSON for conflict, insufficient funds, missing settlement, missing route, and unavailable database errors. The synchronous POST may remain open through the mock five-second timeout because asynchronous admission is explicitly outside scope.

- [ ] **Step 3: Add container startup and PostgreSQL readiness**

Extend `docker-compose.yml` with an API service using `depends_on: condition: service_healthy`. Expose API `8000` and PostgreSQL `5432`; mount a named database volume; pass the async database URL and `PAYOUT_MODE=${PAYOUT_MODE:-random}`.

The Python 3.12 slim image installs the project and runs:

```dockerfile
CMD ["sh", "-c", "alembic upgrade head && python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
```

Run:

```bash
docker compose up --build -d
curl --fail http://localhost:8000/health
pytest tests/integration/test_api.py -q
```

Expected: curl returns `{"status":"ok"}` and all API tests pass.

- [ ] **Step 4: Commit the runnable API slice**

```bash
git add app/main.py app/db.py Dockerfile docker-compose.yml tests/integration/test_api.py
git commit -m "feat: expose settlement API"
```

---

### Task 5: Exact 1,000-Request Verification and Submission Documentation

**Files:**
- Create: `tests/load_test.py`
- Create: `README.md`
- Modify: `app/provider.py`
- Modify: `app/main.py`
- Verify: `ARCHITECTURE.md`
- Verify: `ADR.md`

**Interfaces:**
- Consumes: running Docker Compose API/PostgreSQL stack.
- Produces: load-test proof and complete local-run/submission instructions.

- [ ] **Step 1: Add deterministic load-provider mode**

In load mode, guard an atomic call counter and assign exact outcomes:

```text
calls 0..699   -> PAID
calls 700..849 -> UNPAID
calls 850..999 -> TIMEOUT
```

Operation IDs remain deduplicated, so replay does not increment the counter. Keep the default demo provider at 70/15/15 weighted behavior; enable the exact finite script only with an explicit environment setting used by the load test.

- [ ] **Step 2: Write the load runner before running it**

`tests/load_test.py` must:

1. Require the clean-volume startup command below, then use the seeded `demo-customer` owner with USD 100,000. Abort before sending requests if any settlement already exists for that owner.
2. Send 1,000 concurrent unique-key requests of USD 100 with concurrency 1,000 and `X-Owner-ID: demo-customer`.
3. Wait for all synchronous responses, including the 150 five-second timeouts.
4. Query PostgreSQL read-only and fail nonzero unless all expected values match.

Exact expectations:

```python
EXPECTED = {
    "settlements": 1000,
    "SUCCESS": 700,
    "FAILED": 150,
    "PENDING_RECONCILIATION": 150,
    "RESERVE": 1000,
    "CONSUME": 700,
    "RELEASE": 150,
    "settlement_journals": 1850,
    "total_journals_including_opening": 1851,
    "available_usd": Decimal("15000"),
    "reserved_usd": Decimal("15000"),
    "successful_usd": Decimal("70000"),
}
```

Additionally require zero negative accounts, zero unbalanced journal/currency groups, zero duplicate owner/key rows, zero duplicate settlement/event rows, 1,000 distinct provider operation IDs, and an Omnibus USD credit movement of exactly USD 70,000. The integration idempotency tests—not the external load runner—assert the provider method's raw invocation count.

- [ ] **Step 3: Execute the load proof**

Run:

```bash
docker compose down -v
PAYOUT_MODE=load docker compose up --build -d
python tests/load_test.py --base-url http://localhost:8000 --requests 1000 --concurrency 1000 --amount 100
```

Expected final line:

```text
PASS settlements=1000 success=700 failed=150 pending=150 settlement_journals=1850 available_usd=15000 reserved_usd=15000
```

- [ ] **Step 4: Write README and run the complete verification**

README sections must include:

- `docker compose up --build` quick start and `/health` check.
- Example settlement, status, and reconciliation requests with required headers.
- Migration, unit/integration test, and load-test commands.
- The accepted `O(VE)` routing rationale and request-time lookup complexity.
- USD-only ledger, `503` definitive-failure, and timeout ambiguity assumptions.
- Exactly what remains outside the four-hour scope.
- Links to `ARCHITECTURE.md` and `ADR.md`.
- A submission checklist for repository URL, unedited recording URL, load proof, and ADR.

Run:

```bash
pytest tests/unit -q
pytest tests/integration -q
docker compose up --build -d
curl --fail http://localhost:8000/health
python tests/load_test.py --base-url http://localhost:8000 --requests 1000 --concurrency 1000 --amount 100
```

Expected: zero failed/skipped correctness tests, healthy API, and exact load-test PASS output.

- [ ] **Step 5: Commit the verified submission slice**

```bash
git add app tests README.md ARCHITECTURE.md ADR.md
git commit -m "test: add concurrent settlement verification"
```

## Final Review Checklist

- [ ] `docker compose up --build` succeeds from a clean volume.
- [ ] `pytest` passes against PostgreSQL with no skipped concurrency tests.
- [ ] Routing chooses the maximum receiver output and persists one snapshot version.
- [ ] No transaction remains open across `provider.initiate()` or `provider.lookup()`.
- [ ] Same-key races create one settlement, reservation, and provider operation.
- [ ] Overspend test proves ordered `SELECT FOR UPDATE` locking.
- [ ] Timeout leaves funds reserved and never causes an automatic retry.
- [ ] Reconciliation is lookup-only and consumes/releases at most once.
- [ ] The 1,000-request accounting totals match exactly.
- [ ] ADR and architecture match the implemented state machine and schema.
- [ ] Recording duration remains within four hours.
