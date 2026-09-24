"""payment-service: middle hop of checkout -> payment -> inventory.

Charges an order, then reserves stock for it via inventory-service --
the inter-service call this phase's spec asks for, carrying a propagated
W3C trace context and its own dependency metrics (see
`simulator.services.common.http_client`).
"""

from __future__ import annotations

import asyncio
import random
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from simulator.services.common.chaos import ChaosController
from simulator.services.common.config import ServiceConfig
from simulator.services.common.http_client import call_downstream
from simulator.services.common.telemetry import get_logger, install_observability

config = ServiceConfig.from_env()
chaos = ChaosController(config.redis_url, config.service_name)

app = FastAPI(title=config.service_name)
telemetry = install_observability(app, config, chaos=chaos)
log = get_logger(__name__)

INVENTORY_URL = "http://inventory-service:8003"


class OrderItem(BaseModel):
    sku: str
    quantity: int = 1


class ChargeRequest(BaseModel):
    order_id: str
    amount: float
    currency: str = "USD"
    items: list[OrderItem]


class ChargeResponse(BaseModel):
    order_id: str
    transaction_id: str
    charged: bool


@app.on_event("startup")
async def _startup() -> None:
    chaos.start()
    # A shared, connection-pooling client -- opening a fresh AsyncClient
    # per request churns TCP connections fast enough under sustained load
    # generator traffic to cause genuine (chaos-independent) intermittent
    # ConnectErrors, polluting the error-rate baseline. Caught during Phase
    # 3 verification; see docs/architecture/14-observability-and-chaos.md.
    app.state.http_client = httpx.AsyncClient(timeout=10.0)
    log.info("service.started", version=config.version)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await app.state.http_client.aclose()


@app.get("/ready")
def ready() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/payments/charge", response_model=ChargeResponse)
async def charge(request: ChargeRequest, http_request: Request) -> ChargeResponse:
    request_id = http_request.headers.get("x-request-id")

    latency = chaos.extra_latency_seconds()
    if latency:
        await asyncio.sleep(latency)
    if chaos.should_fail():
        log.error("payment.charge_failed_chaos", order_id=request.order_id)
        raise HTTPException(status_code=500, detail="payment service internal error")

    if request.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")

    # rare, genuine business decline -- independent of chaos
    if random.random() < 0.01:
        log.info("payment.declined", order_id=request.order_id, amount=request.amount)
        raise HTTPException(status_code=402, detail="payment declined")

    for item in request.items:
        response = await call_downstream(
            app.state.http_client,
            telemetry,
            dependency="inventory-service",
            method="POST",
            url=f"{INVENTORY_URL}/inventory/reserve",
            request_id=request_id,
            json={"sku": item.sku, "quantity": item.quantity},
        )
        if response.status_code >= 400:
            log.warning(
                "payment.inventory_reservation_failed",
                order_id=request.order_id,
                sku=item.sku,
                status_code=response.status_code,
            )
            raise HTTPException(
                status_code=502, detail=f"inventory reservation failed for {item.sku}"
            )

    transaction_id = str(uuid.uuid4())
    log.info("payment.charged", order_id=request.order_id, transaction_id=transaction_id)
    return ChargeResponse(order_id=request.order_id, transaction_id=transaction_id, charged=True)
