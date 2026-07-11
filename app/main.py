import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import DatabaseUnavailable, SessionFactory, check_database
from app.ledger import InsufficientFunds
from app.provider import MockPayoutProvider, PayoutProvider
from app.routing import RateBook, RouteNotFound, generate_edges
from app.schemas import SettlementCreate, SettlementRead
from app.service import IdempotencyConflict, SettlementNotFound, SettlementService


DatabaseCheck = Callable[[], Awaitable[None]]


def _provider_from_environment() -> PayoutProvider:
    mode = os.getenv("PAYOUT_MODE", "random")
    if mode not in {"random", "load"}:
        raise RuntimeError(f"unsupported PAYOUT_MODE: {mode}")
    return MockPayoutProvider(load_mode=mode == "load")


def create_app(
    *,
    sessions: async_sessionmaker[AsyncSession] = SessionFactory,
    rates: RateBook | None = None,
    provider: PayoutProvider | None = None,
    database_check: DatabaseCheck | None = None,
) -> FastAPI:
    owns_rates = rates is None
    rates = rates or RateBook()
    provider = provider or _provider_from_environment()
    service = SettlementService(sessions, rates, provider)
    database_check = database_check or (lambda: check_database(sessions))

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if owns_rates:
            rates.publish(generate_edges(1), version=1)
        await rates.start()
        try:
            yield
        finally:
            await rates.stop()

    application = FastAPI(
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    def get_service() -> SettlementService:
        return service

    def get_database_check() -> DatabaseCheck:
        return database_check

    @application.exception_handler(IdempotencyConflict)
    async def idempotency_conflict_handler(
        request: Request, error: IdempotencyConflict
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "idempotency key conflicts with existing settlement"
            },
        )

    @application.exception_handler(InsufficientFunds)
    async def insufficient_funds_handler(
        request: Request, error: InsufficientFunds
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": "insufficient funds"}
        )

    @application.exception_handler(SettlementNotFound)
    async def settlement_not_found_handler(
        request: Request, error: SettlementNotFound
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404, content={"detail": "settlement not found"}
        )

    @application.exception_handler(RouteNotFound)
    async def route_not_found_handler(
        request: Request, error: RouteNotFound
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422, content={"detail": "no route available"}
        )

    @application.exception_handler(DatabaseUnavailable)
    async def database_unavailable_handler(
        request: Request, error: DatabaseUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"detail": "database unavailable"}
        )

    @application.post("/settlements", response_model=SettlementRead)
    async def create_settlement(
        command: SettlementCreate,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1)
        ],
        owner_id: Annotated[str, Header(alias="X-Owner-ID", min_length=1)],
        settlement_service: Annotated[SettlementService, Depends(get_service)],
    ) -> SettlementRead:
        return await settlement_service.create(owner_id, idempotency_key, command)

    @application.get(
        "/settlements/{settlement_id}", response_model=SettlementRead
    )
    async def get_settlement(
        settlement_id: UUID,
        settlement_service: Annotated[SettlementService, Depends(get_service)],
    ) -> SettlementRead:
        return await settlement_service.get(settlement_id)

    @application.post(
        "/settlements/{settlement_id}/reconcile", response_model=SettlementRead
    )
    async def reconcile_settlement(
        settlement_id: UUID,
        settlement_service: Annotated[SettlementService, Depends(get_service)],
    ) -> SettlementRead:
        return await settlement_service.reconcile(settlement_id)

    @application.get("/health")
    async def health(
        database_ready: Annotated[DatabaseCheck, Depends(get_database_check)],
    ) -> dict[str, str]:
        await database_ready()
        return {"status": "ok"}

    return application


app = create_app()
