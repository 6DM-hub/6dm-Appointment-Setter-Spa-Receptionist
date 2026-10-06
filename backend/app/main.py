# backend/app/main.py
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.api import auth
from app.api.v1 import health
from app.api.v1.card_entry import page_router as card_page_router
from app.api.v1.card_entry import router as card_entry_router
from app.core.config import parse_cors_origins, settings
from app.core.database import engine
from app.core.redis import close_redis, init_redis

# Configure Logging
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# OAuth libraries can log Authorization headers and token payloads at DEBUG.
# Keep those namespaces quiet even when application debugging is enabled.
for _sensitive_logger in (
    "requests_oauthlib",
    "oauthlib",
    "google.auth.transport.requests",
):
    logging.getLogger(_sensitive_logger).setLevel(logging.WARNING)
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 Starting {settings.APP_NAME} [{settings.APP_ENV}]")
    logger.info("CORS allowed origins: %s", origins)
    await init_redis()

    # Calls arriving over the SIP trunk bypass our webhooks entirely, so without
    # this reconciliation they never reach `call_logs` and never show on the
    # dashboard. See app/services/twilio_sync.py.
    sync_task: asyncio.Task | None = None
    if settings.TWILIO_SYNC_ENABLED and settings.TWILIO_ACCOUNT_SID:
        from app.services.twilio_sync import run_periodic_sync

        sync_task = asyncio.create_task(run_periodic_sync())

    yield

    logger.info("🛑 Shutting down... cleaning up connections")

    if sync_task is not None:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass

    # Gracefully close Grok service client if initialized
    try:
        from app.services.grok_service import grok_service
        await grok_service.close()
    except (ImportError, AttributeError):
        pass

    await close_redis()
    await engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    # NOTE: deliberately not `debug=settings.DEBUG`. Starlette's debug mode makes
    # ServerErrorMiddleware return the raw traceback as the response body, and that
    # middleware sits *outside* CORSMiddleware — so such responses carry no
    # Access-Control-Allow-Origin header and the browser reports a misleading CORS
    # error instead of the real 500. `catch_unhandled_exceptions` below handles
    # errors inside the CORS layer instead, and logs the traceback server-side.
    lifespan=lifespan,
)

# Parse CORS Origins dynamically from config
origins = parse_cors_origins(settings.CORS_ORIGINS)


# Registered BEFORE CORSMiddleware so that it ends up *inside* it: Starlette builds
# the stack with the most recently added middleware outermost, so CORS headers get
# applied to the error responses produced here.
@app.middleware("http")
async def catch_unhandled_exceptions(request: Request, call_next):
    try:
        return await call_next(request)
    except (RedisError, SQLAlchemyError) as exc:
        logger.error(
            "Backing service unavailable handling %s %s: %s",
            request.method, request.url.path, exc, exc_info=True,
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "A backing service (database or cache) is unavailable."},
        )
    except Exception as exc:
        logger.error(
            "Unhandled error handling %s %s: %s",
            request.method, request.url.path, exc, exc_info=True,
        )
        return JSONResponse(
            status_code=500, content={"detail": "Internal server error"}
        )


app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Core V1 Health Router
app.include_router(health.router, prefix=settings.API_V1_PREFIX)
app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
app.include_router(card_page_router)
app.include_router(card_entry_router, prefix=settings.API_V1_PREFIX)


# Register feature routers individually so one failing module doesn't block the rest
def _load_routers(module_name: str) -> list:
    """Import a router module and return the APIRouters it exposes."""
    if module_name == "telephony":
        from app.api.v1 import telephony
        return [telephony.router]
    if module_name == "xai_voice":
        from app.api.v1 import xai_voice
        return [xai_voice.router]
    if module_name == "contacts":
        from app.api.v1 import contacts
        return [contacts.router]
    if module_name == "appointments":
        from app.api.v1 import appointments
        return [appointments.router]
    if module_name == "call_logs":
        from app.api.v1 import call_logs
        # Same handlers under both /call-logs and /calls.
        return [call_logs.router, call_logs.alias_router]
    if module_name == "spa_accounts":
        from app.api.v1 import spa_accounts
        return [spa_accounts.router]
    if module_name == "services":
        from app.api.v1 import services
        return [services.router]
    if module_name == "leads":
        from app.api.v1 import leads
        return [leads.router]
    if module_name == "analytics":
        from app.api.v1 import analytics
        return [analytics.router]
    raise ValueError(f"Unknown router module {module_name!r}")


for module_name in [
    "telephony",
    "xai_voice",
    "contacts",
    "appointments",
    "call_logs",
    "spa_accounts",
    "services",
    "leads",
    "analytics",
]:
    try:
        for router in _load_routers(module_name):
            app.include_router(router, prefix=settings.API_V1_PREFIX)
        logger.info(f"✅ Registered router: {module_name}")
    except Exception as e:
        logger.error(f"❌ Failed to load router '{module_name}': {e}", exc_info=True)


@app.get("/")
async def root():
    return {
        "message": f"{settings.APP_NAME} API",
        "status": "running",
        "docs": "/docs",
    }