"""checkout-service: public entry point of checkout -> payment -> inventory.

`POST /checkout` is the one endpoint the load generator drives continuously
(see `simulator/services/load-generator/`) -- it's the request that
produces the steady baseline traffic Prometheus's `rate()`-based alert
rules need, and the one whose latency/error rate visibly degrades under
each chaos scenario.
"""

from __future__ import annotations

import asyncio
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

PAYMENT_URL = "http://payment-service:8002"


class OrderItem(BaseModel):
    sku: str
    quantity: int = 1


class CheckoutRequest(BaseModel):
    items: list[OrderItem]
    amount: float
    currency: str = "USD"


class CheckoutResponse(BaseModel):
    order_id: str
    status: str
    transaction_id: str


@app.on_event("startup")
async def _startup() -> None:
    chaos.start()
    # Shared, connection-pooling client -- see payment-service/app.py's
    # identical startup hook for why a fresh AsyncClient per request isn't
    # used here.
    app.state.http_client = httpx.AsyncClient(timeout=10.0)
    log.info("service.started", version=config.version)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await app.state.http_client.aclose()


@app.get("/ready")
def ready() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/checkout", response_model=CheckoutResponse)
async def checkout(request: CheckoutRequest, http_request: Request) -> CheckoutResponse:
    request_id = http_request.headers.get("x-request-id")
    order_id = str(uuid.uuid4())

    latency = chaos.extra_latency_seconds()
    if latency:
        await asyncio.sleep(latency)
    if chaos.should_fail():
        log.error("checkout.failed_chaos", order_id=order_id)
        raise HTTPException(status_code=500, detail="checkout service internal error")

    response = await call_downstream(
        app.state.http_client,
        telemetry,
        dependency="payment-service",
        method="POST",
        url=f"{PAYMENT_URL}/payments/charge",
        request_id=request_id,
        json={
            "order_id": order_id,
            "amount": request.amount,
            "currency": request.currency,
            "items": [item.model_dump() for item in request.items],
        },
    )

    if response.status_code >= 400:
        log.warning("checkout.payment_failed", order_id=order_id, status_code=response.status_code)
        raise HTTPException(status_code=502, detail="payment failed")

    transaction_id = response.json()["transaction_id"]
    log.info("checkout.confirmed", order_id=order_id, transaction_id=transaction_id)
    return CheckoutResponse(order_id=order_id, status="confirmed", transaction_id=transaction_id)
