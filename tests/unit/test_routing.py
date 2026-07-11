import asyncio
from decimal import Decimal

import pytest

from app.routing import (
    Currency,
    Edge,
    InvalidRateGraph,
    RateBook,
    RouteHop,
    RouteNotFound,
    compute_routes,
    generate_edges,
)


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


def test_disconnected_profitable_cycle_does_not_break_reachable_quote():
    edges = (
        Edge(Currency.USD, Currency.PHP, "php", Decimal("55")),
        Edge(Currency.EUR, Currency.AED, "aed", Decimal("4")),
        Edge(Currency.AED, Currency.EUR, "eur", Decimal("0.3")),
    )

    assert compute_routes(edges, version=1)[Currency.PHP].aggregate_rate == Decimal(
        "55"
    )


def test_selects_best_parallel_lp_edge():
    edges = (
        Edge(Currency.USD, Currency.PHP, "LP_A", Decimal("54")),
        Edge(Currency.USD, Currency.PHP, "LP_B", Decimal("56")),
        Edge(Currency.USD, Currency.PHP, "LP_C", Decimal("55")),
    )

    quote = compute_routes(edges, version=2)[Currency.PHP]

    assert quote.aggregate_rate == Decimal("56")
    assert tuple(hop.lp for hop in quote.hops) == ("LP_B",)


def test_equal_parallel_rates_select_smallest_lp_when_input_is_reversed():
    edges = (
        Edge(Currency.USD, Currency.PHP, "LP_A", Decimal("55")),
        Edge(Currency.USD, Currency.PHP, "LP_B", Decimal("55")),
    )

    quote = compute_routes(tuple(reversed(edges)), version=2)[Currency.PHP]

    assert tuple(hop.lp for hop in quote.hops) == ("LP_A",)


def test_equal_product_route_is_stable_when_input_order_is_reversed():
    edges = (
        Edge(Currency.USD, Currency.EUR, "z", Decimal("2")),
        Edge(Currency.EUR, Currency.PHP, "a", Decimal("3")),
        Edge(Currency.USD, Currency.AED, "a", Decimal("3")),
        Edge(Currency.AED, Currency.PHP, "z", Decimal("2")),
    )

    forward = compute_routes(edges, version=3)[Currency.PHP]
    reversed_order = compute_routes(tuple(reversed(edges)), version=3)[Currency.PHP]

    assert forward == reversed_order
    assert tuple(hop.target for hop in forward.hops) == (Currency.AED, Currency.PHP)


@pytest.mark.parametrize("rate", [Decimal("0"), Decimal("-0.01")])
def test_rejects_non_positive_rates(rate):
    edges = (Edge(Currency.USD, Currency.PHP, "LP_A", rate),)

    with pytest.raises(InvalidRateGraph):
        compute_routes(edges, version=1)


def test_omits_disconnected_currency():
    edges = (Edge(Currency.USD, Currency.EUR, "LP_A", Decimal("0.9")),)

    routes = compute_routes(edges, version=4)

    assert Currency.PHP not in routes


def test_rate_book_publishes_and_quotes_one_complete_snapshot():
    book = RateBook()
    edges = (Edge(Currency.USD, Currency.PHP, "LP_A", Decimal("55")),)

    snapshot = book.publish(edges, version=9)
    quote = book.quote(Currency.PHP)

    assert snapshot.version == 9
    assert snapshot.routes[Currency.PHP] is quote
    assert quote.snapshot_version == snapshot.version
    assert quote.target == Currency.PHP
    assert quote.aggregate_rate == Decimal("55")
    assert quote.hops[0] == RouteHop(
        Currency.USD, Currency.PHP, "LP_A", Decimal("55")
    )


def test_invalid_publication_preserves_last_valid_snapshot():
    book = RateBook()
    book.publish(
        (Edge(Currency.USD, Currency.PHP, "LP_A", Decimal("55")),),
        version=1,
    )
    original = book.quote(Currency.PHP)

    with pytest.raises(InvalidRateGraph):
        book.publish(
            (
                Edge(Currency.USD, Currency.PHP, "LP_B", Decimal("56")),
                Edge(Currency.USD, Currency.EUR, "LP_A", Decimal("0.9")),
                Edge(Currency.EUR, Currency.USD, "LP_A", Decimal("1.2")),
            ),
            version=2,
        )

    assert book.quote(Currency.PHP) == original


@pytest.mark.asyncio
async def test_concurrent_readers_only_observe_complete_versions():
    book = RateBook()
    versions = {
        1: (Edge(Currency.USD, Currency.PHP, "direct", Decimal("55")),),
        2: (
            Edge(Currency.USD, Currency.EUR, "eur", Decimal("0.9")),
            Edge(Currency.EUR, Currency.PHP, "php", Decimal("62")),
        ),
    }
    book.publish(versions[1], version=1)
    expected = {
        (
            version,
            quote.hops,
            quote.aggregate_rate,
        )
        for version, edges in versions.items()
        for quote in (compute_routes(edges, version)[Currency.PHP],)
    }
    observed = set()

    async def publish() -> None:
        for index in range(200):
            version = index % 2 + 1
            book.publish(versions[version], version)
            await asyncio.sleep(0)

    async def read() -> None:
        for _ in range(400):
            quote = book.quote(Currency.PHP)
            observed.add(
                (quote.snapshot_version, quote.hops, quote.aggregate_rate)
            )
            await asyncio.sleep(0)

    await asyncio.gather(publish(), *(read() for _ in range(20)))

    assert observed == expected


def test_published_snapshot_routes_cannot_be_mutated():
    book = RateBook()
    edges = (Edge(Currency.USD, Currency.PHP, "LP_A", Decimal("55")),)
    snapshot = book.publish(edges, version=9)

    with pytest.raises(TypeError):
        snapshot.routes[Currency.PHP] = snapshot.routes[Currency.PHP]


def test_rate_book_raises_when_no_snapshot_or_route_exists():
    book = RateBook()

    with pytest.raises(RouteNotFound):
        book.quote(Currency.PHP)

    book.publish((Edge(Currency.USD, Currency.EUR, "LP_A", Decimal("0.9")),), 1)
    with pytest.raises(RouteNotFound):
        book.quote(Currency.PHP)


def test_generate_edges_is_reproducible_and_cycle_safe():
    first = generate_edges(11)
    lps = {"LP_A", "LP_B", "LP_C"}
    expected_quotes = {
        (source, target, lp)
        for source in Currency
        for target in Currency
        if source != target
        for lp in lps
    }

    assert first == generate_edges(11)
    assert first != generate_edges(12)
    covered_currencies = {edge.source for edge in first} | {
        edge.target for edge in first
    }
    assert covered_currencies == set(Currency)
    assert {(edge.source, edge.target, edge.lp) for edge in first} == expected_quotes
    assert len(first) == len(expected_quotes)
    assert all(edge.rate > 0 for edge in first)
    compute_routes(first, version=11)


@pytest.mark.asyncio
async def test_start_publishes_initial_snapshot_once():
    calls = []

    def edge_factory(version):
        calls.append(version)
        return (Edge(Currency.USD, Currency.PHP, "LP_A", Decimal(version)),)

    book = RateBook(edge_factory=edge_factory, interval_seconds=60)
    await book.start()
    await book.start()

    assert calls == [1]
    assert book.quote(Currency.PHP).snapshot_version == 1
    await book.stop()


@pytest.mark.asyncio
async def test_simulator_advances_snapshot_version():
    advanced = asyncio.Event()

    def edge_factory(version):
        if version == 2:
            advanced.set()
        return (Edge(Currency.USD, Currency.PHP, "LP_A", Decimal(version)),)

    book = RateBook(edge_factory=edge_factory, interval_seconds=0.001)
    await book.start()
    await asyncio.wait_for(advanced.wait(), timeout=1)

    assert book.quote(Currency.PHP).snapshot_version >= 2
    await book.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_stops_publication():
    calls = []

    def edge_factory(version):
        calls.append(version)
        return (Edge(Currency.USD, Currency.PHP, "LP_A", Decimal(version)),)

    book = RateBook(edge_factory=edge_factory, interval_seconds=0.001)
    await book.start()
    await asyncio.sleep(0.005)
    await book.stop()
    await book.stop()
    calls_after_stop = len(calls)
    await asyncio.sleep(0.005)

    assert len(calls) == calls_after_stop
