"""Black-box tests for the Dockerized public HTTP API."""

import asyncio
import os
import time
from collections import Counter
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

BASE_URL = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8000")
OWNER = "demo-customer"


def headers(key: str | None = None, owner: str = OWNER) -> dict[str, str]:
    return {
        "Idempotency-Key": key or str(uuid4()),
        "X-Owner-ID": owner,
    }


def assert_route(body: dict) -> None:
    route = body["route"]
    assert route
    assert route[0]["source"] == "USD"
    assert route[-1]["target"] == body["target_currency"]
    for left, right in zip(route, route[1:]):
        assert left["target"] == right["source"]
    product = Decimal("1")
    for hop in route:
        product *= Decimal(hop["rate"])
    assert Decimal(body["aggregate_rate"]) == product.quantize(Decimal("0.00000001"))
    assert Decimal(body["quoted_amount"]) == (
        Decimal(body["amount_usd"]) * product
    ).quantize(Decimal("0.00000001"))


@pytest.mark.boot_contract
@pytest.mark.asyncio
async def test_boot_and_public_contract() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        for path in ("/docs", "/redoc", "/openapi.json", "/not-found"):
            assert (await client.get(path)).status_code == 404

        valid = {"amount_usd": "1", "target_currency": "PHP"}
        assert (await client.post("/settlements", json=valid)).status_code == 422
        assert (
            await client.post(
                "/settlements",
                headers={"Idempotency-Key": ""},
                json=valid,
            )
        ).status_code == 422

        invalid_payloads = (
            {"amount_usd": "0", "target_currency": "PHP"},
            {"amount_usd": "-1", "target_currency": "PHP"},
            {"amount_usd": "1.000000001", "target_currency": "PHP"},
            {"amount_usd": "10000000000000000", "target_currency": "PHP"},
            {"amount_usd": "1", "target_currency": "GBP"},
        )
        for payload in invalid_payloads:
            response = await client.post(
                "/settlements", headers=headers(), json=payload
            )
            assert response.status_code == 422, response.text

        malformed = await client.post(
            "/settlements",
            headers={**headers(), "Content-Type": "application/json"},
            content="{",
        )
        assert malformed.status_code == 422
        assert (await client.get("/settlements/not-a-uuid")).status_code == 422

        maximum = await client.post(
            "/settlements",
            headers=headers(),
            json={
                "amount_usd": "9999999999999999.99999999",
                "target_currency": "PHP",
            },
        )
        assert maximum.status_code == 409
        assert maximum.json() == {"detail": "insufficient funds"}

        absent = str(uuid4())
        assert (await client.get(f"/settlements/{absent}")).status_code == 404
        assert (
            await client.post(f"/settlements/{absent}/reconcile")
        ).status_code == 404


@pytest.mark.settlement_idempotency
@pytest.mark.asyncio
async def test_quote_replay_conflict_and_concurrent_idempotency() -> None:
    timeout = httpx.Timeout(30, pool=30)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
        key = str(uuid4())
        original = await client.post(
            "/settlements",
            headers=headers(key),
            json={"amount_usd": "40.0", "target_currency": "PHP"},
        )
        assert original.status_code == 200, original.text
        body = original.json()
        assert body["status"] == "SUCCESS"
        assert_route(body)

        equivalent = await client.post(
            "/settlements",
            headers=headers(key),
            json={"amount_usd": "40.000", "target_currency": "PHP"},
        )
        assert equivalent.status_code == 200
        assert equivalent.json() == body
        assert (await client.get(f"/settlements/{body['id']}")).json() == body

        for payload in (
            {"amount_usd": "41", "target_currency": "PHP"},
            {"amount_usd": "40", "target_currency": "AED"},
        ):
            conflict = await client.post(
                "/settlements", headers=headers(key), json=payload
            )
            assert conflict.status_code == 409

        concurrent_key = str(uuid4())

        async def replay() -> httpx.Response:
            return await client.post(
                "/settlements",
                headers=headers(concurrent_key),
                json={"amount_usd": "1", "target_currency": "AED"},
            )

        responses = await asyncio.gather(*(replay() for _ in range(100)))
        assert {response.status_code for response in responses} <= {200, 202}
        assert len({response.json()["id"] for response in responses}) == 1
        assert {response.json()["status"] for response in responses} <= {
            "PAYOUT_IN_PROGRESS",
            "SUCCESS",
        }
        settlement_id = responses[0].json()["id"]
        final = await client.get(f"/settlements/{settlement_id}")
        assert final.json()["status"] == "SUCCESS"


@pytest.mark.provider_reconciliation
@pytest.mark.asyncio
async def test_provider_distribution_and_reconciliation() -> None:
    timeout = httpx.Timeout(15, pool=15)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=20)
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=timeout, limits=limits
    ) as client:
        started = time.perf_counter()

        async def create(index: int) -> httpx.Response:
            return await client.post(
                "/settlements",
                headers=headers(f"provider-{index}-{uuid4()}"),
                json={"amount_usd": "10", "target_currency": "PHP"},
            )

        responses = await asyncio.gather(*(create(index) for index in range(20)))
        elapsed = time.perf_counter() - started
        assert elapsed < 15
        assert Counter(response.status_code for response in responses) == {
            200: 14,
            202: 6,
        }
        bodies = [response.json() for response in responses]
        assert Counter(body["status"] for body in bodies) == {
            "SUCCESS": 14,
            "PENDING_RECONCILIATION": 6,
        }

        settlement_ids = {body["id"] for body in bodies}
        deadline = time.monotonic() + 10
        final = []
        while time.monotonic() < deadline:
            fetched = await asyncio.gather(
                *(client.get(f"/settlements/{item}") for item in settlement_ids)
            )
            final = [response.json() for response in fetched]
            if all(body["status"] in {"SUCCESS", "FAILED"} for body in final):
                break
            await asyncio.sleep(0.1)
        assert Counter(body["status"] for body in final) == {
            "SUCCESS": 17,
            "FAILED": 3,
        }

        terminal = final[0]
        terminal_reconcile = await client.post(
            f"/settlements/{terminal['id']}/reconcile"
        )
        assert terminal_reconcile.status_code == 200
        assert terminal_reconcile.json() == terminal


@pytest.mark.concurrency_funds
@pytest.mark.asyncio
async def test_concurrent_requests_cannot_overspend() -> None:
    timeout = httpx.Timeout(60, pool=60)
    limits = httpx.Limits(max_connections=101, max_keepalive_connections=101)
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=timeout, limits=limits
    ) as client:

        async def create(index: int) -> httpx.Response:
            return await client.post(
                "/settlements",
                headers=headers(f"funds-{index}-{uuid4()}"),
                json={"amount_usd": "1000", "target_currency": "AED"},
            )

        responses = await asyncio.gather(*(create(index) for index in range(101)))
        assert Counter(response.status_code for response in responses) == {
            200: 100,
            409: 1,
        }
        assert all(
            response.json()["status"] == "SUCCESS"
            for response in responses
            if response.status_code == 200
        )


@pytest.mark.lifecycle_recovery
@pytest.mark.asyncio
async def test_settlement_is_persisted_for_runner_restart_check() -> None:
    artifact_dir = os.environ.get("E2E_ARTIFACT_DIR")
    assert artifact_dir
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15) as client:
        created = []
        for index in range(18):
            response = await client.post(
                "/settlements",
                headers=headers(f"lifecycle-{index}"),
                json={"amount_usd": "1", "target_currency": "EUR"},
            )
            assert response.status_code in {200, 202}
            created.append(response.json())
        terminal = next(row for row in created if row["status"] == "SUCCESS")
        pending = next(
            row for row in created if row["status"] == "PENDING_RECONCILIATION"
        )
        path = os.path.join(artifact_dir, "lifecycle-settlement-id.txt")
        with open(path, "w", encoding="utf-8") as output:
            output.write(f"{terminal['id']}\n{pending['id']}\n")
