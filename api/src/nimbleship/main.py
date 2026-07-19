import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from nimbleship.labels.store import get_label_store
from nimbleship.legacy.router import router as legacy_router
from nimbleship.routers.assistant import router as assistant_router
from nimbleship.routers.carrier_builder import router as carrier_builder_router
from nimbleship.routers.consignments import router as consignments_router
from nimbleship.routers.definitions import router as definitions_router
from nimbleship.routers.manifests import router as manifests_router
from nimbleship.routers.propositions import router as propositions_router
from nimbleship.routers.quotes import router as quotes_router
from nimbleship.routers.rulebook import router as rulebook_router
from nimbleship.routers.rules_builder import router as rules_builder_router
from nimbleship.routers.service_groups import router as service_groups_router
from nimbleship.routers.shipping_areas import router as shipping_areas_router
from nimbleship.routers.tracking import router as tracking_router
from nimbleship.routers.warehouses import router as warehouses_router

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

    router.include_router(assistant_router)
    router.include_router(carrier_builder_router)
    router.include_router(consignments_router)
    router.include_router(definitions_router)
    router.include_router(manifests_router)
    router.include_router(propositions_router)
    router.include_router(quotes_router)
    router.include_router(rulebook_router)
    router.include_router(rules_builder_router)
    router.include_router(service_groups_router)
    router.include_router(shipping_areas_router)
    router.include_router(tracking_router)
    router.include_router(warehouses_router)
    app.include_router(router)
    # The Legacy Interface mounts outside /api: the WMS posts to the MetaPack
    # service paths (/ConsignmentService, ...) unprefixed.
    app.include_router(legacy_router)
    return app
