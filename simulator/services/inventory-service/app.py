"""inventory-service: leaf node of the checkout -> payment -> inventory chain.

`POST /inventory/reserve` decrements a small in-memory stock table.
Everything below "business logic" (chaos injection, telemetry, request-id/
trace propagation) comes from `simulator.services.common` -- see that
package's docstring for why it doesn't reuse `packages/telemetry`.
"""

from __future__ import annotations

import asyncio
import random

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from simulator.services.common.chaos import ChaosController
from simulator.services.common.config import ServiceConfig
from simulator.services.common.telemetry import get_logger, install_observability

config = ServiceConfig.from_env()
chaos = ChaosController(config.redis_url, config.service_name)

app = FastAPI(title=config.service_name)
telemetry = install_observability(app, config, chaos=chaos)
log = get_logger(__name__)

# Small, deliberately finite stock table: a handful of SKUs run genuinely
# low so an out-of-stock 409 is a real, occasional business-logic response,
# distinct from a chaos-injected failure.
_STOCK: dict[str, int] = {"WIDGET-1": 500, "WIDGET-2": 500, "GADGET-1": 3, "GADGET-2": 500}


class ReserveRequest(BaseModel):
    sku: str
    quantity: int = 1


class ReserveResponse(BaseModel):
    sku: str
    quantity: int
    reserved: bool


@app.on_event("startup")
async def _startup() -> None:
    chaos.start()
    log.info("service.started", version=config.version)


@app.get("/ready")
def ready() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/inventory/reserve", response_model=ReserveResponse)
async def reserve(request: ReserveRequest) -> ReserveResponse:
    latency = chaos.extra_latency_seconds()
    if latency:
        await asyncio.sleep(latency)
    if chaos.should_fail():
        log.error("inventory.reserve_failed_chaos", sku=request.sku)
        raise HTTPException(status_code=500, detail="inventory service internal error")

    # Restock is rolled unconditionally, before the availability check: a
    # depleted SKU must still have a chance to recover on a *failed*
    # request, or it gets stuck at 0 forever once it first hits empty
    # (every subsequent request 409s before ever reaching a "restock on
    # success" branch) -- a genuine bug caught during Phase 3 verification
    # (docs/architecture/14-observability-and-chaos.md), not a chaos effect.
    if random.random() < 0.1:
        _STOCK[request.sku] = min(_STOCK.get(request.sku, 200) + 50, 500)

    available = _STOCK.get(request.sku, 200)
    if available < request.quantity:
        log.warning("inventory.out_of_stock", sku=request.sku, available=available)
        raise HTTPException(status_code=409, detail=f"insufficient stock for {request.sku}")

    _STOCK[request.sku] = available - request.quantity

    log.info("inventory.reserved", sku=request.sku, quantity=request.quantity)
    return ReserveResponse(sku=request.sku, quantity=request.quantity, reserved=True)
