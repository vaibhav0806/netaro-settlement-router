"""Deterministic FX routing and in-memory rate snapshots.

Snapshot construction uses Bellman-Ford-style relaxation and is O(VE), an
intentional difference from the general O(V+E) target in the specification.
Serving a quote from a published snapshot is O(1), plus use of its route hops.
"""

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from app.decimal_policy import isolated_decimal


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


Candidate = tuple[Decimal, tuple[RouteHop, ...]]


def _hop_key(hops: tuple[RouteHop, ...]) -> tuple[tuple[str, str, str], ...]:
    return tuple((hop.source.value, hop.target.value, hop.lp) for hop in hops)


def _is_better(candidate: Candidate, current: Candidate | None) -> bool:
    if current is None or candidate[0] > current[0]:
        return True
    return candidate[0] == current[0] and _hop_key(candidate[1]) < _hop_key(current[1])


def _collapsed_edges(edges: tuple[Edge, ...]) -> tuple[Edge, ...]:
    selected: dict[tuple[Currency, Currency], Edge] = {}
    for edge in sorted(
        edges, key=lambda item: (item.source.value, item.target.value, item.lp)
    ):
        if not edge.rate.is_finite() or edge.rate <= 0:
            raise InvalidRateGraph("rates must be positive and finite")
        key = (edge.source, edge.target)
        current = selected.get(key)
        if current is None or edge.rate > current.rate:
            selected[key] = edge
    return tuple(selected.values())


def _relax(
    best: dict[Currency, Candidate], edges: tuple[Edge, ...]
) -> dict[Currency, Candidate]:
    next_best = best.copy()
    for edge in edges:
        source = best.get(edge.source)
        if source is None:
            continue
        hop = RouteHop(edge.source, edge.target, edge.lp, edge.rate)
        candidate = (source[0] * edge.rate, source[1] + (hop,))
        if _is_better(candidate, next_best.get(edge.target)):
            next_best[edge.target] = candidate
    return next_best


def _compute_routes(
    edges: tuple[Edge, ...],
    version: int,
    source: Currency = Currency.USD,
) -> Mapping[Currency, RouteQuote]:
    """Return maximum-product routes, rejecting invalid/profitable cycles."""
    collapsed = _collapsed_edges(edges)
    best: dict[Currency, Candidate] = {source: (Decimal("1"), ())}

    for _ in range(len(Currency) - 1):
        next_best = _relax(best, collapsed)
        if next_best == best:
            break
        best = next_best

    extra = _relax(best, collapsed)
    if any(
        target not in best or candidate[0] > best[target][0]
        for target, candidate in extra.items()
    ):
        raise InvalidRateGraph("profitable cycle is reachable from source")

    return MappingProxyType(
        {
            target: RouteQuote(version, target, aggregate_rate, hops)
            for target, (aggregate_rate, hops) in best.items()
            if target != source
        }
    )


def compute_routes(
    edges: tuple[Edge, ...],
    version: int,
    source: Currency = Currency.USD,
) -> Mapping[Currency, RouteQuote]:
    return isolated_decimal(lambda: _compute_routes(edges, version, source))


def _generate_edges(version: int) -> tuple[Edge, ...]:
    base_anchors = {
        Currency.USD: Decimal("1"),
        Currency.USDC: Decimal("1"),
        Currency.EUR: Decimal("0.92"),
        Currency.PHP: Decimal("55"),
        Currency.AED: Decimal("3.67"),
    }
    coefficients = {
        Currency.USD: 1,
        Currency.USDC: 2,
        Currency.EUR: 3,
        Currency.PHP: 4,
        Currency.AED: 5,
    }
    anchors = {
        currency: anchor
        * (
            Decimal("1")
            + Decimal((version * coefficients[currency]) % 7 - 3) / Decimal("10000")
        )
        for currency, anchor in base_anchors.items()
    }
    lp_factors = {
        "LP_A": Decimal("0.999"),
        "LP_B": Decimal("0.998"),
        "LP_C": Decimal("0.997"),
    }
    return tuple(
        Edge(source, target, lp, anchors[target] / anchors[source] * factor)
        for source in Currency
        for target in Currency
        if source != target
        for lp, factor in lp_factors.items()
    )


def generate_edges(version: int) -> tuple[Edge, ...]:
    return isolated_decimal(lambda: _generate_edges(version))


class RateBook:
    def __init__(
        self,
        edge_factory: Callable[[int], tuple[Edge, ...]] = generate_edges,
        interval_seconds: float = 0.05,
    ) -> None:
        self._edge_factory = edge_factory
        self._interval_seconds = interval_seconds
        self._snapshot: RateSnapshot | None = None
        self._task: asyncio.Task[None] | None = None

    def publish(self, edges: tuple[Edge, ...], version: int) -> RateSnapshot:
        snapshot = RateSnapshot(version, compute_routes(edges, version))
        self._snapshot = snapshot
        return snapshot

    def quote(self, target: Currency) -> RouteQuote:
        if self._snapshot is None or target not in self._snapshot.routes:
            raise RouteNotFound(target)
        return self._snapshot.routes[target]

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._snapshot is None:
            self.publish(self._edge_factory(1), 1)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            assert self._snapshot is not None
            version = self._snapshot.version + 1
            self.publish(self._edge_factory(version), version)
