import base64
import hmac
import logging
import re
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Config
from .delivery import Delivery
from .imaging import decode_icon
from .publishing import Publisher
from .store import DomainError, Store

logger = logging.getLogger(__name__)
ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class UnknownUpload(StrictModel):
    image_base64: str = Field(max_length=350_000)
    client_revision: int = Field(default=0, ge=0)
    scene: str = Field(default="", max_length=64)
    client: str = Field(default="", max_length=64)


class UnknownBatch(StrictModel):
    candidates: list[UnknownUpload] = Field(min_length=1, max_length=50)


class MappingBody(StrictModel):
    item_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    tag: str = Field(default="", max_length=32)

    @field_validator("item_id")
    @classmethod
    def validate_id(cls, value):
        if value != value.strip() or not ITEM_ID.fullmatch(value) or value.startswith("U-"):
            raise ValueError("Use a stable formal item ID; U- is reserved for temporary identities")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        if not value.strip():
            raise ValueError("Display name cannot be blank")
        return value.strip()


class FormalUpload(MappingBody):
    image_base64: str = Field(max_length=350_000)


class PublishBody(StrictModel):
    note: str = Field(default="Administrator mapping publication", max_length=500)


class BodyLimit:
    """Bound request memory before Pydantic or the PNG decoder sees the payload."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        limit = 256 * 1024 if scope["path"].endswith("unknown-candidates") else 512 * 1024
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

    app = FastAPI(title="AzurPilot Template Service", version="0.1.0", lifespan=lifespan)
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

    def admin_auth(authorization: Annotated[str | None, Header()] = None):
        authenticate(config.admin_token, authorization)

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

    @app.get("/internal/v1/templates/events", dependencies=[Depends(event_auth)])
    def events(after: int = Query(default=0, ge=0)):
        return {"events": jsonable_encoder(store.events(after))}

    @app.get("/internal/v1/templates/status", dependencies=[Depends(admin_auth)])
    def status():
        return {
            **store.status(),
            "deploy_mode": config.deploy_mode,
            "public_base": config.public_base,
            "main_event_configured": bool(config.main_event_url and config.event_token),
            "public_key": base64.b64encode(publisher.key.public_key().public_bytes_raw()).decode(),
        }

    @app.get("/internal/v1/templates", dependencies=[Depends(admin_auth)])
    def templates(
        kind: Literal["formal", "temporary"] | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        return {"items": store.templates(kind, limit, offset)}

    @app.get("/internal/v1/templates/icons/{sha256}", dependencies=[Depends(admin_auth)])
    def icon_preview(sha256: str):
        if not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise DomainError("Icon not found", 404)
        data = store.icon_png(sha256)
        if data is None:
            raise DomainError("Icon not found", 404)
        return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/internal/v1/templates", dependencies=[Depends(admin_auth)])
    def import_formal(body: FormalUpload):
        metadata = body.model_dump(exclude={"image_base64"})
        return store.ingest(decode_icon(body.image_base64, allow_cell=True), formal=metadata)

    @app.put("/internal/v1/templates/mappings/{unknown_id}", dependencies=[Depends(admin_auth)])
    def save_mapping(unknown_id: str, body: MappingBody):
        store.save_mapping(unknown_id, body.model_dump())
        return {"saved": True, "published": False}

    @app.post("/internal/v1/templates/publish", status_code=202, dependencies=[Depends(admin_auth)])
    def publish(body: PublishBody):
        return {"request_id": store.request_publish(body.note), "status": "queued"}

    @app.get("/internal/v1/templates/releases", dependencies=[Depends(admin_auth)])
    def releases():
        return {"items": store.releases()}

    return app
