from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from hypothesis import given, settings
from hypothesis import strategies as st

from app.routing import Currency, Edge, compute_routes, generate_edges

ORACLE_CONTEXT = Context(prec=38, rounding=ROUND_HALF_EVEN)


def _oracle_rate(edges: tuple[Edge, ...], target: Currency) -> Decimal:
    best: tuple[Decimal, tuple[tuple[str, str, str], ...]] | None = None

    def visit(
        current: Currency,
        amount: Decimal,
        visited: frozenset[Currency],
        path: tuple[tuple[str, str, str], ...],
    ) -> None:
        nonlocal best
        if current == target:
            candidate = (amount, path)
            if (
                best is None
                or candidate[0] > best[0]
                or (candidate[0] == best[0] and candidate[1] < best[1])
            ):
                best = candidate
            return
        for edge in edges:
            if edge.source != current or edge.target in visited:
                continue
            visit(
                edge.target,
                amount * edge.rate,
                visited | {edge.target},
                path + ((edge.source.value, edge.target.value, edge.lp),),
            )

    with localcontext(ORACLE_CONTEXT):
        visit(Currency.USD, Decimal("1"), frozenset({Currency.USD}), ())
    assert best is not None
    return best[0]


@settings(max_examples=50, deadline=None)
@given(
    version=st.integers(min_value=1, max_value=10_000),
    target=st.sampled_from([Currency.USDC, Currency.EUR, Currency.PHP, Currency.AED]),
)
def test_bellman_ford_matches_independent_simple_path_oracle(
    version: int,
    target: Currency,
) -> None:
    edges = generate_edges(version)

    actual = compute_routes(edges, version)[target]

    assert actual.aggregate_rate == _oracle_rate(edges, target)
