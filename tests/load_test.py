#!/usr/bin/env python3
"""External 1,000-request HTTP and read-only PostgreSQL verification."""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from decimal import Decimal
from uuid import uuid4

import asyncpg
import httpx

from app.routing import Currency, compute_routes, generate_edges


OWNER_ID = "demo-customer"
EXPECTED = {
    "settlements": 1000,
    "SUCCESS": 700,
    "FAILED": 150,
    "PENDING_RECONCILIATION": 150,
    "RESERVE": 1000,
    "CONSUME": 700,
    "RELEASE": 150,
    "OPENING": 1,
    "settlement_journals": 1850,
    "total_journals_including_opening": 1851,
    "available_usd": Decimal("15000"),
    "reserved_usd": Decimal("15000"),
    "successful_usd": Decimal("70000"),
}
MONEY_QUANTUM = Decimal("0.00000001")


def require_equal(label: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def http_timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=10, write=30, read=120, pool=120)


async def preflight(database_url: str) -> None:
    connection = await asyncpg.connect(database_url)
    try:
        async with connection.transaction(readonly=True):
            settlements = await connection.fetchval(
                "SELECT count(*) FROM settlements WHERE owner_id = $1", OWNER_ID
            )
            if settlements:
                raise RuntimeError(
                    f"clean volume required: {settlements} settlements already exist "
                    f"for {OWNER_ID}"
                )
            balances = {
                row["purpose"]: row["balance"]
                for row in await connection.fetch(
                    """
                    SELECT purpose::text, balance
                    FROM accounts
                    WHERE owner_id = $1 AND currency = 'USD'
                    """,
                    OWNER_ID,
                )
            }
            require_equal(
                "preflight available USD",
                balances.get("AVAILABLE"),
                Decimal("100000"),
            )
            require_equal(
                "preflight reserved USD", balances.get("RESERVED"), Decimal("0")
            )
    finally:
        await connection.close()


async def send_requests(
    base_url: str, requests: int, concurrency: int, amount: Decimal
) -> list[dict]:
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        base_url=base_url, limits=limits, timeout=http_timeout()
    ) as client:

        async def send_one() -> dict:
            key = str(uuid4())
            async with semaphore:
                response = await client.post(
                    "/settlements",
                    headers={
                        "Idempotency-Key": key,
                        "X-Owner-ID": OWNER_ID,
                    },
                    json={"amount_usd": str(amount), "target_currency": "PHP"},
                )
            if response.status_code != 200:
                raise AssertionError(
                    f"HTTP settlement failure: {response.status_code} {response.text}"
                )
            return response.json()

        return await asyncio.gather(*(send_one() for _ in range(requests)))


def verify_route(row: asyncpg.Record) -> None:
    version = row["snapshot_version"]
    target = Currency(row["target_currency"])
    expected = compute_routes(generate_edges(version), version)[target]
    stored_route = row["route"]
    if isinstance(stored_route, str):
        stored_route = json.loads(stored_route)

    expected_route = [
        {
            "source": hop.source.value,
            "target": hop.target.value,
            "lp": hop.lp,
            "rate": str(hop.rate),
        }
        for hop in expected.hops
    ]
    require_equal(f"route for {row['id']}", stored_route, expected_route)
    require_equal(
        f"aggregate rate for {row['id']}",
        row["aggregate_rate"],
        expected.aggregate_rate.quantize(MONEY_QUANTUM),
    )
    require_equal(
        f"quoted output for {row['id']}",
        row["quoted_amount"],
        (row["amount_usd"] * expected.aggregate_rate).quantize(MONEY_QUANTUM),
    )


async def audit(database_url: str) -> dict[str, object]:
    connection = await asyncpg.connect(database_url)
    try:
        async with connection.transaction(readonly=True):
            settlements = await connection.fetch(
                """
                SELECT id, status::text, amount_usd, target_currency::text,
                       route, snapshot_version, aggregate_rate, quoted_amount,
                       provider_operation_id
                FROM settlements
                WHERE owner_id = $1
                ORDER BY id
                """,
                OWNER_ID,
            )
            require_equal("settlements", len(settlements), EXPECTED["settlements"])

            statuses = Counter(row["status"] for row in settlements)
            expected_statuses = Counter(
                {
                    status: EXPECTED[status]
                    for status in (
                        "SUCCESS",
                        "FAILED",
                        "PENDING_RECONCILIATION",
                    )
                }
            )
            require_equal("settlement statuses", statuses, expected_statuses)

            operation_ids = {row["provider_operation_id"] for row in settlements}
            require_equal("distinct provider operation IDs", len(operation_ids), 1000)
            require_equal(
                "provider operation ID mismatches",
                sum(row["provider_operation_id"] != row["id"] for row in settlements),
                0,
            )

            versions = {row["snapshot_version"] for row in settlements}
            if len(versions) < 2:
                raise AssertionError(
                    "routing proof requires at least two distinct snapshot versions"
                )
            for row in settlements:
                verify_route(row)

            event_rows = await connection.fetch(
                """
                SELECT journal.event::text, count(*) AS count
                FROM journal_transactions AS journal
                JOIN settlements AS settlement ON settlement.id = journal.settlement_id
                WHERE settlement.owner_id = $1
                GROUP BY journal.event
                """,
                OWNER_ID,
            )
            events = {row["event"]: row["count"] for row in event_rows}
            for event in ("RESERVE", "CONSUME", "RELEASE"):
                require_equal(event, events.get(event, 0), EXPECTED[event])
            require_equal(
                "settlement journals",
                sum(events.values()),
                EXPECTED["settlement_journals"],
            )
            require_equal(
                "total journals including opening",
                await connection.fetchval("SELECT count(*) FROM journal_transactions"),
                EXPECTED["total_journals_including_opening"],
            )
            opening_journals = await connection.fetchval(
                """
                SELECT count(*)
                FROM journal_transactions
                WHERE event = 'OPENING' AND settlement_id IS NULL
                """
            )
            require_equal(
                "OPENING journals with NULL settlement",
                opening_journals,
                EXPECTED["OPENING"],
            )

            balances = {
                row["purpose"]: row["balance"]
                for row in await connection.fetch(
                    """
                    SELECT purpose::text, balance
                    FROM accounts
                    WHERE owner_id = $1 AND currency = 'USD'
                    """,
                    OWNER_ID,
                )
            }
            require_equal(
                "available USD",
                balances.get("AVAILABLE"),
                EXPECTED["available_usd"],
            )
            require_equal(
                "reserved USD",
                balances.get("RESERVED"),
                EXPECTED["reserved_usd"],
            )
            require_equal(
                "successful USD",
                await connection.fetchval(
                    """
                    SELECT coalesce(sum(amount_usd), 0)
                    FROM settlements
                    WHERE owner_id = $1 AND status = 'SUCCESS'
                    """,
                    OWNER_ID,
                ),
                EXPECTED["successful_usd"],
            )
            require_equal(
                "Omnibus USD credit movement",
                await connection.fetchval(
                    """
                    SELECT coalesce(sum(posting.amount), 0)
                    FROM postings AS posting
                    JOIN accounts AS account ON account.id = posting.account_id
                    JOIN journal_transactions AS journal
                      ON journal.id = posting.journal_id
                    WHERE account.owner_id = 'system'
                      AND account.purpose = 'OMNIBUS'
                      AND posting.currency = 'USD'
                      AND posting.side = 'CREDIT'
                      AND journal.event = 'CONSUME'
                    """
                ),
                EXPECTED["successful_usd"],
            )

            invariant_queries = {
                "negative accounts": "SELECT count(*) FROM accounts WHERE balance < 0",
                "unbalanced journal/currency groups": """
                    SELECT count(*) FROM (
                        SELECT journal_id, currency
                        FROM postings
                        GROUP BY journal_id, currency
                        HAVING coalesce(sum(amount) FILTER (WHERE side = 'DEBIT'), 0)
                             <> coalesce(sum(amount) FILTER (WHERE side = 'CREDIT'), 0)
                    ) AS invalid
                """,
                "duplicate owner/key rows": """
                    SELECT count(*) FROM (
                        SELECT owner_id, idempotency_key
                        FROM settlements
                        GROUP BY owner_id, idempotency_key
                        HAVING count(*) > 1
                    ) AS duplicates
                """,
                "duplicate settlement/event rows": """
                    SELECT count(*) FROM (
                        SELECT settlement_id, event
                        FROM journal_transactions
                        WHERE settlement_id IS NOT NULL
                        GROUP BY settlement_id, event
                        HAVING count(*) > 1
                    ) AS duplicates
                """,
            }
            for label, query in invariant_queries.items():
                require_equal(label, await connection.fetchval(query), 0)

            return {
                "settlements": len(settlements),
                "statuses": statuses,
                "settlement_journals": sum(events.values()),
                "available_usd": balances["AVAILABLE"],
                "reserved_usd": balances["RESERVED"],
                "snapshot_versions": len(versions),
                "opening_journals": opening_journals,
            }
    finally:
        await connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--requests", type=int, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--amount", type=Decimal, required=True)
    default_port = os.getenv("POSTGRES_HOST_PORT", "5432")
    parser.add_argument(
        "--database-url",
        default=os.getenv(
            "LOAD_DATABASE_URL",
            f"postgresql://netaro:netaro@localhost:{default_port}/netaro",
        ),
    )
    args = parser.parse_args()
    if (args.requests, args.concurrency, args.amount) != (1000, 1000, Decimal("100")):
        parser.error(
            "the accounting proof requires "
            "--requests 1000 --concurrency 1000 --amount 100"
        )
    return args


async def run(args: argparse.Namespace) -> None:
    await preflight(args.database_url)
    started = time.perf_counter()
    responses = await send_requests(
        args.base_url, args.requests, args.concurrency, args.amount
    )
    elapsed = time.perf_counter() - started
    require_equal(
        "unique HTTP settlement IDs",
        len({item["id"] for item in responses}),
        1000,
    )
    result = await audit(args.database_url)
    statuses = result["statuses"]
    print(
        f"completed_requests=1000 elapsed_seconds={elapsed:.2f} "
        f"snapshot_versions={result['snapshot_versions']} "
        f"opening_journals={result['opening_journals']}"
    )
    print(
        "PASS "
        f"settlements={result['settlements']} "
        f"success={statuses['SUCCESS']} "
        f"failed={statuses['FAILED']} "
        f"pending={statuses['PENDING_RECONCILIATION']} "
        f"settlement_journals={result['settlement_journals']} "
        f"available_usd={result['available_usd']:.0f} "
        f"reserved_usd={result['reserved_usd']:.0f}"
    )


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except Exception as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
