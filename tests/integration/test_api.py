from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.exc import SQLAlchemyError

from app.db import DatabaseUnavailable, check_database
from app.provider import PayoutTimeout, ProviderLookup, ProviderResult
from app.routing import Currency, Edge, RateBook
from app.main import create_app
from conftest import ScriptedPayoutProvider


async def test_app_exposes_only_approved_routes(session_factory):
    app = create_app(
        sessions=session_factory,
        provider=ScriptedPayoutProvider(ProviderResult.PAID),
    )

    routes = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }

    assert routes == {
        ("POST", "/settlements"),
        ("GET", "/settlements/{settlement_id}"),
        ("POST", "/settlements/{settlement_id}/reconcile"),
        ("GET", "/health"),
    }


async def test_check_database_wraps_sqlalchemy_failure_and_closes_session():
    class FailingSession:
        def __init__(self) -> None:
            self.entered = False
            self.executed = False
            self.exited = False
            self.closed = False

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, exception_type, exception, traceback):
            self.exited = True
            await self.close()

        async def close(self) -> None:
            self.closed = True

        async def execute(self, statement):
            self.executed = True
            raise SQLAlchemyError("database down")

    class FailingSessionFactory:
        def __init__(self) -> None:
            self.called = False
            self.session = FailingSession()

        def __call__(self):
            self.called = True
            return self.session

    sessions = FailingSessionFactory()

    with pytest.raises(DatabaseUnavailable) as captured:
        await check_database(sessions)

    assert isinstance(captured.value.__cause__, SQLAlchemyError)
    assert sessions.called
    assert sessions.session.entered
    assert sessions.session.executed
    assert sessions.session.exited
    assert sessions.session.closed


@pytest_asyncio.fixture
async def client_factory(seeded_accounts, session_factory, rate_book):
    clients: list[httpx.AsyncClient] = []
    lifespans: list[AsyncIterator[None]] = []

    async def make_client(
        provider=None,
        *,
        rates=rate_book,
        database_check=None,
        raise_app_exceptions=True,
    ) -> httpx.AsyncClient:
        provider = provider or ScriptedPayoutProvider(ProviderResult.PAID)
        app = create_app(
            sessions=session_factory,
            rates=rates,
            provider=provider,
            database_check=database_check,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        lifespans.append(lifespan)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app, raise_app_exceptions=raise_app_exceptions
            ),
            base_url="http://test",
        )
        clients.append(client)
        return client

    yield make_client

    for client in reversed(clients):
        await client.aclose()
    for lifespan in reversed(lifespans):
        await lifespan.__aexit__(None, None, None)


async def test_health_checks_postgres(client_factory):
    client = await client_factory()

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_returns_503_when_postgres_is_unavailable(client_factory):
    async def unavailable() -> None:
        raise DatabaseUnavailable

    client = await client_factory(database_check=unavailable)

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


async def test_create_requires_idempotency_and_owner_headers(client_factory):
    client = await client_factory()

    response = await client.post(
        "/settlements", json={"amount_usd": "40", "target_currency": "PHP"}
    )

    assert response.status_code == 422


async def test_post_creates_and_replays_exact_quote(client_factory):
    provider = ScriptedPayoutProvider(ProviderResult.PAID)
    client = await client_factory(provider)
    headers = {"Idempotency-Key": "api-paid", "X-Owner-ID": "customer"}
    payload = {"amount_usd": "40.12345678", "target_currency": "PHP"}

    created = await client.post("/settlements", headers=headers, json=payload)
    replay = await client.post("/settlements", headers=headers, json=payload)

    assert created.status_code == replay.status_code == 200
    assert replay.json() == created.json()
    assert created.json() == {
        "id": created.json()["id"],
        "status": "SUCCESS",
        "amount_usd": "40.12345678",
        "target_currency": "PHP",
        "quoted_amount": "2206.79012290",
        "aggregate_rate": "55.00000000",
        "snapshot_version": 7,
        "route": [
            {
                "source": "USD",
                "target": "PHP",
                "lp": "LP_TEST",
                "rate": "55",
            }
        ],
    }
    assert len(provider.initiate_calls) == 1


async def test_get_returns_created_settlement(client_factory):
    client = await client_factory()
    created = await client.post(
        "/settlements",
        headers={"Idempotency-Key": "api-get", "X-Owner-ID": "customer"},
        json={"amount_usd": "40", "target_currency": "PHP"},
    )

    response = await client.get(f"/settlements/{created.json()['id']}")

    assert response.status_code == 200
    assert response.json() == created.json()


async def test_reconciliation_is_idempotent(client_factory):
    provider = ScriptedPayoutProvider(
        PayoutTimeout(), (ProviderLookup.PAID, ProviderLookup.UNPAID)
    )
    client = await client_factory(provider)
    created = await client.post(
        "/settlements",
        headers={"Idempotency-Key": "api-reconcile", "X-Owner-ID": "customer"},
        json={"amount_usd": "40", "target_currency": "PHP"},
    )

    first = await client.post(f"/settlements/{created.json()['id']}/reconcile")
    replay = await client.post(f"/settlements/{created.json()['id']}/reconcile")

    assert created.json()["status"] == "PENDING_RECONCILIATION"
    assert first.status_code == replay.status_code == 200
    assert first.json()["status"] == replay.json()["status"] == "SUCCESS"
    assert len(provider.lookup_calls) == 1


async def test_changed_idempotent_payload_returns_stable_409(client_factory):
    client = await client_factory()
    headers = {"Idempotency-Key": "api-conflict", "X-Owner-ID": "customer"}
    await client.post(
        "/settlements",
        headers=headers,
        json={"amount_usd": "40", "target_currency": "PHP"},
    )

    response = await client.post(
        "/settlements",
        headers=headers,
        json={"amount_usd": "41", "target_currency": "PHP"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "idempotency key conflicts with existing settlement"
    }


@pytest.mark.parametrize("method", ["get", "post"])
async def test_missing_settlement_returns_stable_404(client_factory, method):
    client = await client_factory()
    path = f"/settlements/{uuid4()}"
    if method == "post":
        path += "/reconcile"

    response = await getattr(client, method)(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "settlement not found"}


async def test_insufficient_funds_returns_stable_409(client_factory):
    client = await client_factory()

    response = await client.post(
        "/settlements",
        headers={"Idempotency-Key": "api-funds", "X-Owner-ID": "customer"},
        json={"amount_usd": "1001", "target_currency": "PHP"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "insufficient funds"}


async def test_largest_accepted_amount_returns_insufficient_funds(client_factory):
    client = await client_factory(raise_app_exceptions=False)

    response = await client.post(
        "/settlements",
        headers={"Idempotency-Key": "api-max-funds", "X-Owner-ID": "customer"},
        json={
            "amount_usd": "9999999999999999.99999999",
            "target_currency": "PHP",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "insufficient funds"}


async def test_missing_route_returns_stable_422(client_factory):
    rates = RateBook()
    rates.publish(
        (Edge(Currency.USD, Currency.PHP, "ONLY_PHP", Decimal("55")),),
        version=11,
    )
    client = await client_factory(rates=rates)

    response = await client.post(
        "/settlements",
        headers={"Idempotency-Key": "api-route", "X-Owner-ID": "customer"},
        json={"amount_usd": "40", "target_currency": "AED"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "no route available"}


async def test_lifespan_publishes_initial_rates_and_stops_task(session_factory):
    rates = RateBook(interval_seconds=60)
    app = create_app(
        sessions=session_factory,
        rates=rates,
        provider=ScriptedPayoutProvider(ProviderResult.PAID),
        database_check=lambda: check_database(session_factory),
    )

    async with app.router.lifespan_context(app):
        assert rates.quote(Currency.PHP).snapshot_version == 1
        assert rates._task is not None and not rates._task.done()

    assert rates._task is None
