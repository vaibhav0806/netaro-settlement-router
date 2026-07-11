import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest

from app.main import _provider_from_environment
from app.provider import MockPayoutProvider, PayoutTimeout, ProviderResult
from app.routing import Currency


async def _initiate(provider, settlement_id):
    try:
        return await provider.initiate(
            settlement_id,
            Decimal("100"),
            Currency.PHP,
            Decimal("5500"),
        )
    except PayoutTimeout:
        return None


async def test_load_mode_assigns_exact_outcomes_under_concurrency(monkeypatch):
    monkeypatch.setenv("PAYOUT_MODE", "load")
    provider = _provider_from_environment()

    results = await asyncio.gather(
        *(_initiate(provider, uuid4()) for _ in range(1000))
    )

    assert results.count(ProviderResult.PAID) == 700
    assert results.count(ProviderResult.UNPAID) == 150
    assert results.count(None) == 150


async def test_load_mode_uses_contiguous_outcome_ranges():
    provider = MockPayoutProvider(timeout_seconds=0, load_mode=True)

    results = [await _initiate(provider, uuid4()) for _ in range(1000)]

    assert results[:700] == [ProviderResult.PAID] * 700
    assert results[700:850] == [ProviderResult.UNPAID] * 150
    assert results[850:] == [None] * 150


async def test_load_mode_deduplicates_replayed_uuid(monkeypatch):
    monkeypatch.setenv("PAYOUT_MODE", "load")
    provider = _provider_from_environment()
    settlement_id = uuid4()

    first, replay = await asyncio.gather(
        _initiate(provider, settlement_id),
        _initiate(provider, settlement_id),
    )
    remaining = await asyncio.gather(
        *(_initiate(provider, uuid4()) for _ in range(999))
    )

    assert first == replay == ProviderResult.PAID
    assert remaining.count(ProviderResult.PAID) == 699
    assert remaining.count(ProviderResult.UNPAID) == 150
    assert remaining.count(None) == 150


async def test_load_mode_rejects_more_than_one_thousand_unique_operations(
    monkeypatch,
):
    monkeypatch.setenv("PAYOUT_MODE", "load")
    provider = _provider_from_environment()
    await asyncio.gather(
        *(_initiate(provider, uuid4()) for _ in range(1000))
    )

    with pytest.raises(RuntimeError, match="load provider supports exactly 1000"):
        await _initiate(provider, uuid4())
