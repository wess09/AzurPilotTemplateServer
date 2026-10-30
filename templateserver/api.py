import hmac
import logging
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .admin import AdminSocket
from .config import Config
from .delivery import Delivery
from .imaging import decode_icon, decode_screenshot
from .publishing import Publisher
from .schemas import ScreenshotUpload, UnknownBatch, UnknownUpload
from .store import DomainError, Store

logger = logging.getLogger(__name__)


class BodyLimit:
    """Bound request memory before Pydantic or the PNG decoder sees the payload."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        limit = 256 * 1024 if scope["path"].endswith("unknown-candidates") else 512 * 1024
        if scope["path"] == "/api/v3/post/templates/screenshots":
            limit = 6 * 1024 * 1024
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > limit:
                return await JSONResponse(
                    {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Request too large"}}, status_code=413
                )(scope, receive, send)
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


class RateLimiter:
    def __init__(self, clock=time.monotonic):
        self.entries = OrderedDict()
        self.mutex = threading.Lock()
        self.clock = clock

    def check(self, token: str):
        with self.mutex:
            now = self.clock()
            previous, available = self.entries.pop(token, (now, 50.0))
            available = min(50.0, available + (now - previous) * 10)
            if len(self.entries) >= 10_000:
                self.entries.popitem(last=False)
            self.entries[token] = (now, max(0, available - 1))
            if available < 1:
                raise DomainError("Upload rate exceeded; retry later", 429)


def create_app(
    config: Config | None = None, store=None, *, background=True, publisher=None, rate_limiter=None
):
    config = config or Config.from_env()
    store = store or Store(config.database_url)
    publisher = publisher or Publisher(store, config)
    delivery = Delivery(store, config)
    stop = threading.Event()
    rate = rate_limiter or RateLimiter()

    def run_publication():
        failures = 0
        while not stop.is_set():
            delay = config.auto_publish_seconds
            try:
                publisher.run_once()
                failures = 0
            except Exception:
                logger.exception("Template publication failed; retry pending")
                failures += 1
                delay = min(300, delay * 2 ** min(failures, 8))
            stop.wait(delay)

    def run_delivery():
        while not stop.is_set():
            try:
                if delivery.run_once():
                    continue
            except Exception:
                logger.exception("Template event delivery failed; retry pending")
            stop.wait(0.25)

    @asynccontextmanager
    async def lifespan(app):
        store.open()
        threads = []
        if background:
            threads = [
                threading.Thread(target=run_publication, name="template-publisher"),
                threading.Thread(target=run_delivery, name="template-delivery"),
            ]
            for thread in threads:
                thread.start()
        yield
        stop.set()
        for thread in threads:
            thread.join()  # Network and deployment calls have bounded timeouts.
        delivery.close()
        store.close()

    app = FastAPI(
        title="AzurPilot Template Service",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyLimit)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.admin_origins),
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type", "X-AzurPilot-ID"],
        expose_headers=["Retry-After"],
        max_age=600,
    )
    app.state.store, app.state.publisher = store, publisher

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        headers = {"Retry-After": "1"} if exc.status == 429 else None
        return JSONResponse(
            {"error": {"code": "TEMPLATE_ERROR", "message": exc.message}},
            status_code=exc.status,
            headers=headers,
        )

    @app.exception_handler(ValueError)
    async def invalid_icon(request, exc):
        return JSONResponse({"error": {"code": "INVALID_ICON", "message": str(exc)}}, status_code=400)

    def authenticate(expected, authorization):
        if not expected:
            raise DomainError("Endpoint is not configured", 503)
        if not hmac.compare_digest((authorization or "").encode(), ("Bearer " + expected).encode()):
            raise DomainError("Invalid service credential", 401)

    def ingest_limit(request: Request, x_azurpilot_id: Annotated[str, Header()] = ""):
        # Public client uploads never require a shared server credential. Do not trust arbitrary
        # forwarded IP headers; the socket peer bucket also bounds clients rotating device IDs.
        peer = request.client.host if request.client else "unknown"
        rate.check("peer:" + peer)
        if x_azurpilot_id:
            rate.check("device:" + x_azurpilot_id[:128])

    def event_auth(authorization: Annotated[str | None, Header()] = None):
        authenticate(config.event_token, authorization)

    @app.get("/healthz")
    def health():
        return {"service": "templates", "revision": store.status()["revision"]}

    def upload_result(body: UnknownUpload, icon=None):
        result = store.ingest(icon or decode_icon(body.image_base64))
        # An unknown upload can reveal an incomplete cache even at the reported current revision.
        result["update_required"] = True
        result["update_url"] = config.public_base + "/latest.json"
        return result

    @app.post("/api/v3/post/templates/unknown", dependencies=[Depends(ingest_limit)])
    def unknown(body: UnknownUpload):
        return upload_result(body)

    @app.post("/api/v3/post/unknown-candidates", dependencies=[Depends(ingest_limit)])
    def unknown_batch(body: UnknownBatch):
        icons = [decode_icon(row.image_base64) for row in body.candidates]
        return {
            "results": [upload_result(row, icon) for row, icon in zip(body.candidates, icons, strict=True)]
        }

    @app.post("/api/v3/post/templates/screenshots", dependencies=[Depends(ingest_limit)])
    def screenshot(body: ScreenshotUpload):
        digest, png = decode_screenshot(body.image_base64)
        return store.save_screenshot(
            digest, png, body.scene, body.client, [row.model_dump() for row in body.references]
        )

    @app.get("/internal/v1/templates/events", dependencies=[Depends(event_auth)])
    def events(after: int = Query(default=0, ge=0)):
        return {"events": jsonable_encoder(store.events(after))}

    administration = AdminSocket(store, publisher, config)
    app.websocket("/internal/v1/templates/ws")(administration.connect)

    return app
