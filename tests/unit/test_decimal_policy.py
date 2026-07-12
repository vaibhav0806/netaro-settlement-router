from decimal import localcontext

from app.routing import Currency, compute_routes, generate_edges


def test_routing_is_isolated_from_global_decimal_context() -> None:
    expected = compute_routes(generate_edges(17), 17)[Currency.PHP]

    with localcontext() as context:
        context.prec = 6
        observed = compute_routes(generate_edges(17), 17)[Currency.PHP]

    assert observed == expected
