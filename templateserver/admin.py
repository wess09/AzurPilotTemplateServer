"""Direct browser management over authenticated WebSocket; no REST administration."""

import asyncio
import base64
import hmac
import json
import logging
import time
from contextlib import suppress
from typing import Literal

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from pydantic import Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from .schemas import FormalUpload, MappingBody, PublishBody, SaveItem, StrictModel
from .store import DomainError


class Authenticate(StrictModel):
    type: Literal["auth"]
    token: str = Field(max_length=4096)


class Command(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    method: str = Field(max_length=80)
    params: dict = Field(default_factory=dict)


class ListTemplates(StrictModel):
    kind: Literal["formal", "temporary"] | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    discarded: bool = False


class DiscardTemplate(StrictModel):
    identity_id: str = Field(min_length=1, max_length=128)
    discarded: bool


class ListItems(StrictModel):
    limit: int = Field(default=200, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class SaveMapping(MappingBody):
    unknown_id: str = Field(min_length=3, max_length=128)


class ReadIcon(StrictModel):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ListScreenshots(StrictModel):
    identity_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0)


class ReadScreenshot(ReadIcon):
    offset: int = Field(default=0, ge=0, lt=4 * 1024 * 1024)
    length: int = Field(default=192 * 1024, ge=1, le=192 * 1024)


class AdminSocket:
    def __init__(self, store, publisher, config):
        self.store, self.publisher, self.config = store, publisher, config

    def snapshot(self):
        return jsonable_encoder(
            {
                **self.store.status(),
                "deploy_mode": self.config.deploy_mode,
                "public_base": self.config.public_base,
                "main_event_configured": bool(self.config.main_event_url and self.config.event_token),
                "public_key": base64.b64encode(self.publisher.key.public_key().public_bytes_raw()).decode(),
            }
        )

    def execute(self, method: str, params: dict):
        if method in {"state.get", "state.subscribe", "status.get", "releases.list", "ping"}:
            StrictModel.model_validate(params)
        if method in {"state.get", "state.subscribe", "status.get"}:
            return self.snapshot()
        if method == "templates.list":
            body = ListTemplates.model_validate(params)
            return {"items": self.store.templates(body.kind, body.limit, body.offset, body.discarded)}
        if method == "items.list":
            body = ListItems.model_validate(params)
            return {"items": self.store.items(body.limit, body.offset)}
        if method == "items.save":
            body = SaveItem.model_validate(params)
            return self.store.save_item(body.model_dump(exclude={"tag"}))
        if method == "templates.discard":
            body = DiscardTemplate.model_validate(params)
            return self.store.set_discarded(body.identity_id, body.discarded)
        if method == "templates.import":
            from .imaging import decode_icon

            body = FormalUpload.model_validate(params)
            return self.store.ingest(
                decode_icon(body.image_base64, allow_cell=True),
                formal=body.model_dump(exclude={"image_base64"}),
            )
        if method == "mappings.save":
            body = SaveMapping.model_validate(params)
            self.store.save_mapping(body.unknown_id, body.model_dump(exclude={"unknown_id"}))
            return {"saved": True, "published": False}
        if method == "publish":
            body = PublishBody.model_validate(params)
            return {"request_id": self.store.request_publish(body.note), "status": "queued"}
        if method == "releases.list":
            return {"items": self.store.releases()}
        if method == "icons.get":
            body = ReadIcon.model_validate(params)
            png = self.store.icon_png(body.sha256)
            if png is None:
                raise DomainError("Icon not found", 404)
            return {"image_base64": base64.b64encode(png).decode()}
        if method == "screenshots.list":
            body = ListScreenshots.model_validate(params)
            return {"items": self.store.screenshots(body.identity_id, body.limit, body.offset)}
        if method == "screenshots.get":
            body = ReadScreenshot.model_validate(params)
            return self.store.screenshot_chunk(body.sha256, body.offset, body.length)
        if method == "ping":
            return {"pong": True}
        raise DomainError("Management method not found", 404)

    async def connect(self, websocket: WebSocket):
        if websocket.headers.get("origin") not in self.config.admin_origins or websocket.query_params:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        lock = asyncio.Lock()

        async def send(value):
            async with lock:
                await asyncio.wait_for(websocket.send_json(jsonable_encoder(value)), timeout=10)

        try:
            auth = Authenticate.model_validate(await asyncio.wait_for(websocket.receive_json(), timeout=5))
            if not self.config.admin_token or not hmac.compare_digest(
                auth.token.encode(), self.config.admin_token.encode()
            ):
                await send(
                    {
                        "type": "error",
                        "error": {"code": "UNAUTHORIZED", "message": "管理凭据无效", "status": 401},
                    }
                )
                await websocket.close(code=1008)
                return
        except (TimeoutError, ValidationError, ValueError, WebSocketDisconnect):
            await websocket.close(code=1008)
            return
        del auth
        await send({"type": "authenticated"})
        subscribed = asyncio.Event()

        async def push_changes():
            last = None
            try:
                while True:
                    await subscribed.wait()
                    snapshot = await asyncio.to_thread(self.snapshot)
                    encoded = json.dumps(snapshot, sort_keys=True)
                    if encoded != last:
                        await send({"type": "event", "topic": "state", "data": snapshot})
                        last = encoded
                    await asyncio.sleep(2)
            except WebSocketDisconnect:
                pass
            except (SQLAlchemyError, OSError, TimeoutError, RuntimeError):
                logging.getLogger(__name__).error("Template state subscription failed")
                with suppress(RuntimeError, WebSocketDisconnect):
                    await websocket.close(code=1011)

        task = asyncio.create_task(push_changes())
        available, previous = 50.0, time.monotonic()
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw.encode()) > 512 * 1024:
                    await websocket.close(code=1009)
                    return
                request_id = None
                try:
                    command = Command.model_validate_json(raw)
                    request_id = command.id
                    now = time.monotonic()
                    available = min(50.0, available + (now - previous) * 10)
                    previous = now
                    if available < 1:
                        raise DomainError("Management rate exceeded", 429)
                    available -= 1
                    result = await asyncio.to_thread(self.execute, command.method, command.params)
                    await send({"id": command.id, "type": "result", "data": result})
                    if command.method == "state.subscribe":
                        subscribed.set()
                except ValidationError:
                    await send(
                        {
                            "id": request_id,
                            "type": "error",
                            "error": {
                                "code": "VALIDATION_ERROR",
                                "message": "管理命令或参数不合法",
                                "status": 422,
                            },
                        }
                    )
                except (DomainError, ValueError) as exc:
                    await send(
                        {
                            "id": request_id,
                            "type": "error",
                            "error": {
                                "code": "TEMPLATE_ERROR",
                                "message": str(exc),
                                "status": getattr(exc, "status", 400),
                            },
                        }
                    )
                except (SQLAlchemyError, OSError, TimeoutError):
                    logging.getLogger(__name__).error("Template management command failed")
                    await send(
                        {
                            "id": request_id,
                            "type": "error",
                            "error": {"code": "INTERNAL", "message": "管理操作失败", "status": 500},
                        }
                    )
        except WebSocketDisconnect:
            pass
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect):
                await task
