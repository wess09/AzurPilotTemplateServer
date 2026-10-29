"""Exercise two independent local services and the Worker static distribution end to end."""

import base64
import json
import time
import uuid
from datetime import UTC, datetime

import cv2
import httpx
import numpy as np
from sqlalchemy import create_engine, select

from templateserver.signing import verify


def main():
    main_url, template_url = "http://127.0.0.1:18096", "http://127.0.0.1:18093"
    headers = {"Authorization": "Bearer main-admin"}
    admin = main_url + "/api/v1/admin/template-service"
    client = httpx.Client(timeout=15, trust_env=False)
    # Synthetic fixture only: no player screenshots or account data.
    image = np.random.default_rng(int(uuid.uuid4().hex[:8], 16)).integers(
        20, 230, (49, 49, 3), dtype=np.uint8
    )
    encoded = base64.b64encode(cv2.imencode(".png", image)[1]).decode()
    uploaded = client.post(
        main_url + "/api/v3/post/templates/unknown",
        json={"image_base64": encoded, "scene": "future-statistics", "client_revision": 0},
    )
    uploaded.raise_for_status()
    unknown = uploaded.json()["template_id"]
    assert unknown.startswith("U-")
    key = base64.b64decode(client.get(admin + "/status", headers=headers).json()["public_key"])
    for _ in range(80):
        latest_document = client.get("http://127.0.0.1:8787/latest.json")
        latest = verify(latest_document.json(), key)
        current = client.get(latest["manifest_url"])
        if current.status_code != 200:
            time.sleep(0.25)
            continue
        manifest = verify(current.json(), key)
        if any(row["template_id"] == unknown and row["kind"] == "temporary" for row in manifest["templates"]):
            break
        time.sleep(0.25)
    else:
        raise AssertionError("Temporary template did not reach Worker")
    record_prefix = "e2e-" + uuid.uuid4().hex[:16]
    events = [
        {
            "record_id": record_prefix + f"-{index:03d}",
            "scene": "commission",
            "server": "cn",
            "status": 0,
            "collected_at": datetime.now(UTC).isoformat(),
            "comm": "Template E2E Escort",
            "name": "模板闭环测试",
            "items": [{"item": unknown, "tag": "", "amount": 12}],
        }
        for index in range(51)
    ]
    received = client.post(main_url + "/api/v3/post/drops", json={"drops": events})
    received.raise_for_status()
    assert all(row["status"] == "accepted" for row in received.json()["results"])
    response = client.put(
        admin + "/mappings/" + unknown,
        headers=headers,
        json={"item_id": "BlueprintE2E", "name": "测试图纸", "tag": ""},
    )
    response.raise_for_status()
    response = client.post(admin + "/publish", headers=headers, json={"note": "本地完整闭环验证"})
    response.raise_for_status()
    for _ in range(80):
        status = client.get(admin + "/status", headers=headers).json()
        if status["drafts"] == 0 and status["pending_events"] == 0:
            break
        time.sleep(0.25)
    else:
        raise AssertionError("Publication or main acknowledgement timed out")
    known = client.post(main_url + "/api/v3/post/templates/unknown", json={"image_base64": encoded}).json()
    assert known["template_id"] == "BlueprintE2E" and known["match"] == "formal"
    key = base64.b64decode(status["public_key"])
    latest_response = client.get("http://127.0.0.1:8787/latest.json")
    latest_response.raise_for_status()
    latest = verify(latest_response.json(), key)
    manifest_response = client.get(latest["manifest_url"])
    manifest_response.raise_for_status()
    manifest = verify(manifest_response.json(), key)
    assert any(
        row["unknown_id"] == unknown and row["item_id"] == "BlueprintE2E" for row in manifest["replace_ids"]
    )
    # Check main data through ORM only; original records keep their identity and amounts.
    from sqlalchemy import MetaData, Table

    engine = create_engine("postgresql+psycopg://template_test@127.0.0.1:55439/postgres")
    metadata = MetaData(schema="main_template_live")
    items = Table("commission_items", metadata, autoload_with=engine)
    aggregates = Table("commission_item_hourly", metadata, autoload_with=engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(select(items.c.amount).where(items.c.record_id == events[0]["record_id"])) == 12
        )
        result = connection.execute(
            select(aggregates.c.amount, aggregates.c.occurrences).where(
                aggregates.c.comm == "Template E2E Escort", aggregates.c.item == "BlueprintE2E"
            )
        ).first()
        assert result and result.amount >= 612 and result.occurrences >= 51
    report = {
        "unknown_id": unknown,
        "formal_id": "BlueprintE2E",
        "revision": latest["revision"],
        "main_acknowledged": True,
        "historical_events_verified": 51,
        "worker_manifest_signature_verified": True,
        "separate_template_url": template_url,
    }
    from pathlib import Path

    Path("test-results").mkdir(exist_ok=True)
    Path("test-results/e2e.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    client.close()
    engine.dispose()


if __name__ == "__main__":
    main()
