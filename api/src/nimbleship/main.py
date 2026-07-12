import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from nimbleship.labels.store import get_label_store
from nimbleship.routers.consignments import router as consignments_router
from nimbleship.routers.rulebook import router as rulebook_router
from nimbleship.routers.shipping_areas import router as shipping_areas_router

# Every route lives under /api: the ingress forwards the prefix unstripped,
# so the app owns it rather than relying on proxy rewrites.
API_PREFIX = "/api"

_PRUNE_INTERVAL_SECONDS = 24 * 60 * 60


async def _prune_labels_daily() -> None:
    while True:
        await asyncio.sleep(_PRUNE_INTERVAL_SECONDS)
        get_label_store().prune()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_label_store().prune()
    prune_task = asyncio.create_task(_prune_labels_daily())
    try:
        yield
    finally:
        prune_task.cancel()


def create_app() -> FastAPI:
    app = FastAPI(
        title="NimbleShip",
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
        lifespan=_lifespan,
    )
    router = APIRouter(prefix=API_PREFIX)

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    router.include_router(consignments_router)
    router.include_router(rulebook_router)
    router.include_router(shipping_areas_router)
    app.include_router(router)
    return app
