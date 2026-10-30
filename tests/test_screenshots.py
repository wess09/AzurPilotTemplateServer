import base64
import hashlib

import cv2
import numpy as np
import pytest
from conftest import encoded_image
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from templateserver.api import create_app
from templateserver.imaging import decode_screenshot
from templateserver.models import Screenshot, ScreenshotReference


def frame():
    image = np.random.default_rng(80).integers(0, 255, (720, 1280, 3), np.uint8)
    return cv2.imencode(".png", image)[1].tobytes()


def test_original_png_is_preserved_and_dimensions_are_bounded():
    png = frame()
    assert decode_screenshot(base64.b64encode(png).decode()) == (hashlib.sha256(png).hexdigest(), png)
    for data in ("bad base64", encoded_image(), base64.b64encode(b"x" * (4 * 1024 * 1024 + 1)).decode()):
        with pytest.raises(ValueError):
            decode_screenshot(data)


def test_full_upload_deduplication_private_ws_chunks_and_release_isolation(store, config):
    app = create_app(config, store=store, background=False)
    png = frame()
    digest = hashlib.sha256(png).hexdigest()
    with TestClient(app) as client:
        ids = [
            client.post("/api/v3/post/templates/unknown", json={"image_base64": encoded_image(seed)}).json()[
                "identity_id"
            ]
            for seed in (1, 2)
        ]
        data = {
            "image_base64": base64.b64encode(png).decode(),
            "scene": "research",
            "references": [
                {"identity_id": ids[0], "bounds": [100, 200, 196, 296]},
                {"identity_id": ids[1], "bounds": [200, 200, 296, 296]},
            ],
        }
        for _ in range(2):
            response = client.post("/api/v3/post/templates/screenshots", json=data)
            assert response.status_code == 200, response.text
            assert response.json() == {"stored": True, "sha256": digest}
        with store.session() as session:
            assert session.scalar(select(func.count()).select_from(Screenshot)) == 1
            assert session.scalar(select(func.count()).select_from(ScreenshotReference)) == 2
        with client.websocket_connect(
            "/internal/v1/templates/ws", headers={"origin": "https://admin.azurpilot.com"}
        ) as ws:
            ws.send_json({"type": "auth", "token": "test-admin"})
            assert ws.receive_json()["type"] == "authenticated"
            ws.send_json({"id": "list", "method": "screenshots.list", "params": {"identity_id": ids[0]}})
            row = ws.receive_json()["data"]["items"][0]
            assert row["sha256"] == digest and row["bounds"] == [100, 200, 196, 296]
            received = bytearray()
            while len(received) < len(png):
                ws.send_json(
                    {
                        "id": "chunk",
                        "method": "screenshots.get",
                        "params": {"sha256": digest, "offset": len(received)},
                    }
                )
                chunk = ws.receive_json()["data"]
                assert chunk["offset"] == len(received) and chunk["total_size"] == len(png)
                received.extend(base64.b64decode(chunk["data_base64"]))
            assert bytes(received) == png
        assert store.templates(None, 50, 0)[0]["screenshot_count"] == 1
        assert client.get("/internal/v1/templates/screenshots/" + digest).status_code == 404
        app.state.publisher.run_once()
        assert digest not in str(list(config.data_dir.rglob("*")))
        assert "image_base64" not in str(store.events(0))


def test_bad_references_roll_back_and_request_limits(store, config):
    with TestClient(create_app(config, store=store, background=False)) as client:
        data = {
            "image_base64": base64.b64encode(frame()).decode(),
            "scene": "commission",
            "references": [{"identity_id": "U-MISSING", "bounds": [0, 0, 96, 96]}],
        }
        assert client.post("/api/v3/post/templates/screenshots", json=data).status_code == 404
        with store.session() as session:
            assert session.scalar(select(func.count()).select_from(Screenshot)) == 0
        data["references"][0]["bounds"] = [0, 0, 1281, 96]
        assert client.post("/api/v3/post/templates/screenshots", json=data).status_code == 422
        assert (
            client.post(
                "/api/v3/post/templates/screenshots", content=b"x" * (6 * 1024 * 1024 + 1)
            ).status_code
            == 413
        )
