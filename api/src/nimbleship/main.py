from fastapi import APIRouter, FastAPI

# Every route lives under /api: the ingress forwards the prefix unstripped,
# so the app owns it rather than relying on proxy rewrites.
API_PREFIX = "/api"


def create_app() -> FastAPI:
    app = FastAPI(
        title="NimbleShip",
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    router = APIRouter(prefix=API_PREFIX)

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app
