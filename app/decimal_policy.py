from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Context, localcontext

ROUTING_CONTEXT = Context(prec=38, rounding=ROUND_HALF_EVEN)


def isolated_decimal[T](operation: Callable[[], T]) -> T:
    with localcontext(ROUTING_CONTEXT):
        return operation()
