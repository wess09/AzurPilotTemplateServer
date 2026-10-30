import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from conftest import encoded_image
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from templateserver.api import RateLimiter, create_app
from templateserver.delivery import Delivery
from templateserver.imaging import decode_icon
from templateserver.models import IconImage, MappingDraft, OutboxEvent, TemplateIdentity
from templateserver.publishing import Publisher
from templateserver.signing import verify
from templateserver.store import utcnow


def test_concurrent_same_icon_one_identity_and_no_statistics_tables(store):
    icon = decode_icon(encoded_image())
    with ThreadPoolExecutor(max_workers=24) as executor:
        results = list(executor.map(lambda _: store.ingest(icon), range(100)))
    assert len({row["template_id"] for row in results}) == 1
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(TemplateIdentity)) == 1
        assert session.scalar(select(func.count()).select_from(IconImage)) == 1


def test_concurrent_distinct_icons_do_not_merge(store):
    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(lambda n: store.ingest(decode_icon(encoded_image(n))), range(30)))
    assert len({row["template_id"] for row in results}) == 30


def test_mapping_draft_isolated_and_publication_replaces_id(store, config):
    first = store.ingest(decode_icon(encoded_image()))
    publisher = Publisher(store, config)
    revision = publisher.run_once()
    mapping = {"item_id": "BlueprintT3", "name": "测试图纸", "tag": "test"}
    store.save_mapping(first["template_id"], mapping)
    # An unrelated auto publication must not expose the administrator's draft.
    store.ingest(decode_icon(encoded_image(22)))
    publisher.run_once()
    current = store.ingest(decode_icon(encoded_image()))
    assert current["template_id"] == first["template_id"]
    store.request_publish("Approve blueprint mapping")
    published = publisher.run_once()
    assert published > revision
    result = store.ingest(decode_icon(encoded_image()))
    assert result["match"] == "formal" and result["template_id"] == "BlueprintT3"
    manifest = json.loads((config.data_dir / "public" / "manifest.json").read_bytes())
    payload = verify(manifest, publisher.key.public_key().public_bytes_raw())
    assert payload["replace_ids"][0]["unknown_id"] == first["template_id"]
    assert payload["replace_ids"][0]["item_id"] == "BlueprintT3"
    entry = next(row for row in payload["templates"] if row["identity_id"] == first["identity_id"])
    assert entry["template_id"] == "BlueprintT3" and entry["kind"] == "formal"
    change = verify(
        json.loads(
            (
                config.data_dir / "public" / "releases" / str(published) / "deltas" / f"{revision}.json"
            ).read_bytes()
        ),
        publisher.key.public_key().public_bytes_raw(),
    )
    assert change["replace_ids"] == payload["replace_ids"]


def test_approval_snapshot_does_not_publish_later_edits(store, config):
    key = store.ingest(decode_icon(encoded_image()))["template_id"]
    approved = {"item_id": "ItemA", "name": "A", "tag": ""}
    store.save_mapping(key, approved)
    store.request_publish("A only")
    store.save_mapping(key, {"item_id": "ItemB", "name": "B", "tag": ""})
    Publisher(store, config).run_once()
    assert store.ingest(decode_icon(encoded_image()))["template_id"] == "ItemA"
    with store.session() as session:
        assert session.get(MappingDraft, key).item_id == "ItemB"


def test_failed_deployment_retains_revision_and_resumes_same_snapshot(store, config):
    store.ingest(decode_icon(encoded_image()))
    initial_revision = Publisher(store, config).run_once()
    old_manifest = (config.data_dir / "public" / "manifest.json").read_bytes()
    old_latest = (config.data_dir / "public" / "latest.json").read_bytes()
    store.ingest(decode_icon(encoded_image(27)))

    def fail(*args):
        raise RuntimeError("network offline")

    cloud = replace(config, deploy_mode="cloudflare")
    publisher = Publisher(store, cloud, deploy=fail)
    with pytest.raises(RuntimeError):
        publisher.run_once()
    assert store.status()["revision"] == initial_revision
    assert (config.data_dir / "public" / "manifest.json").read_bytes() == old_manifest
    assert (config.data_dir / "public" / "latest.json").read_bytes() == old_latest
    with pytest.raises(RuntimeError):
        publisher.run_once()
    with store.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "release.failed")
            )
            == 1
        )
    failed_revision = store.releases()[0]["revision"]
    publisher.deploy = lambda *args: None
    assert publisher.run_once() == failed_revision
    assert store.status()["revision"] == failed_revision


def test_interrupted_ingest_rolls_back_identity_icon_and_event(store, monkeypatch):
    import templateserver.store as repository

    def interrupted(*args):
        raise RuntimeError("simulated process interruption before commit")

    monkeypatch.setattr(repository, "emit", interrupted)
    with pytest.raises(RuntimeError):
        store.ingest(decode_icon(encoded_image()))
    with store.session() as session:
        for model in (TemplateIdentity, IconImage, OutboxEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_concurrent_publishers_only_activate_one_revision(store, config):
    store.ingest(decode_icon(encoded_image()))
    publisher = Publisher(store, config)
    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(lambda _: publisher.run_once(), range(5)))
    assert len(store.releases()) == 1
    assert store.status()["revision"] == store.releases()[0]["revision"]


def test_delivery_requires_matching_ack_and_retries_in_order(store, config):
    store.ingest(decode_icon(encoded_image()))
    Publisher(store, config).run_once()
    seen = []

    def receiver(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers["Authorization"] == "Bearer test-events"
        return httpx.Response(200, json={"ack": "wrong" if len(seen) == 1 else body["event_id"]})

    delivery = Delivery(store, config, httpx.Client(transport=httpx.MockTransport(receiver)))
    assert not delivery.run_once()
    with store.session.begin() as session:
        row = session.scalar(select(OutboxEvent).order_by(OutboxEvent.sequence))
        assert row.acknowledged_at is None
        row.next_attempt = utcnow()
    assert delivery.run_once()
    assert seen[0]["event_id"] == seen[1]["event_id"]
    assert delivery.run_once()
    assert seen[-1]["type"] == "release.published"
    delivery.close()


def test_api_auth_validation_scene_independence_and_update_signal(store, config):
    app = create_app(config, store=store, background=False)
    with TestClient(app) as client:
        endpoint = "/api/v3/post/templates/unknown"
        data = {"image_base64": encoded_image(), "scene": "future-scene"}
        response = client.post(endpoint, json=data)
        assert response.status_code == 200
        first = response.json()
        data["scene"] = "another-scene"
        second = client.post(endpoint, json=data).json()
        assert first["template_id"] == second["template_id"]
        assert first["update_required"] and first["update_url"].endswith("latest.json")
        assert client.post(endpoint, json={"image_base64": "wrong"}).status_code == 400
        assert client.post(endpoint, content="x" * (513 * 1024)).status_code == 413
        assert (
            client.put(
                "/internal/v1/templates/mappings/" + first["template_id"],
                headers={"Authorization": "Bearer test-admin"},
                json={"item_id": "U-invalid", "name": "wrong"},
            ).status_code
            == 422
        )


def test_signature_tamper_rejected_and_key_persists(store, config):
    store.ingest(decode_icon(encoded_image()))
    publisher = Publisher(store, config)
    publisher.run_once()
    assert publisher.key.private_bytes_raw() == Publisher(store, config).key.private_bytes_raw()
    document = json.loads((config.data_dir / "public" / "latest.json").read_bytes())
    document["payload_base64"] = document["payload_base64"][:-4] + "AAAA"
    from cryptography.exceptions import InvalidSignature

    with pytest.raises(InvalidSignature):
        verify(document, publisher.key.public_key().public_bytes_raw())


def test_known_icon_always_notifies_refresh_even_at_current_revision(store, config):
    app = create_app(config, store=store, background=False)
    with TestClient(app) as client:
        image = encoded_image()
        response = client.post(
            "/internal/v1/templates",
            headers={"Authorization": "Bearer test-admin"},
            json={"item_id": "KnownBlueprint", "name": "已知图纸", "image_base64": image},
        )
        assert response.status_code == 200
        revision = app.state.publisher.run_once()
        response = client.post(
            "/api/v3/post/templates/unknown",
            json={"image_base64": image, "client_revision": revision},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["match"] == "formal" and result["template_id"] == "KnownBlueprint"
        assert result["update_required"] and not result["publication_pending"]


def test_direct_admin_cors_authentication_and_error_responses(store, config):
    app = create_app(config, store=store, background=False)
    origin = "https://api.azurpilot.com"
    with TestClient(app) as client:
        preflight = client.options(
            "/internal/v1/templates/status",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin
        denied = client.options(
            "/internal/v1/templates/status",
            headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "GET"},
        )
        assert denied.status_code == 400
        assert "access-control-allow-origin" not in denied.headers
        unauthenticated = client.get("/internal/v1/templates/status", headers={"Origin": origin})
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["access-control-allow-origin"] == origin
        authenticated = client.get(
            "/internal/v1/templates/status",
            headers={"Origin": origin, "Authorization": "Bearer test-admin"},
        )
        assert authenticated.status_code == 200
        assert authenticated.headers["access-control-allow-origin"] == origin
        too_large = client.post(
            "/internal/v1/templates",
            headers={"Origin": origin, "Authorization": "Bearer test-admin"},
            content="x" * (513 * 1024),
        )
        assert too_large.status_code == 413
        assert too_large.headers["access-control-allow-origin"] == origin


def test_public_upload_rate_limit_cannot_be_bypassed_with_rotating_device_ids(store, config):
    app = create_app(config, store=store, background=False, rate_limiter=RateLimiter(clock=lambda: 0.0))
    with TestClient(app) as client:
        for index in range(50):
            response = client.post(
                "/api/v3/post/templates/unknown",
                headers={"X-AzurPilot-ID": str(index)},
                json={"image_base64": "not-a-png"},
            )
            assert response.status_code == 400
        response = client.post(
            "/api/v3/post/templates/unknown",
            headers={"X-AzurPilot-ID": "new-device"},
            json={"image_base64": "not-a-png"},
        )
        assert response.status_code == 429 and response.headers["retry-after"] == "1"


def test_public_batch_upload_needs_no_service_credential(store, config):
    image = encoded_image()
    with TestClient(create_app(config, store=store, background=False)) as client:
        response = client.post(
            "/api/v3/post/unknown-candidates",
            json={"candidates": [{"image_base64": image}, {"image_base64": image}]},
        )
        assert response.status_code == 200
        first, second = response.json()["results"]
        assert first["template_id"] == second["template_id"]
        assert first["match"] == "new" and second["match"] == "cluster"


def test_main_notifications_only_carry_changed_mappings_and_never_template_images(store, config):
    first = store.ingest(decode_icon(encoded_image(10)))
    publisher = Publisher(store, config)
    publisher.run_once()
    mapping = {"item_id": "BlueprintT3", "name": "图纸", "tag": ""}
    store.save_mapping(first["template_id"], mapping)
    store.request_publish("Approve one alias")
    mapped_revision = publisher.run_once()
    publications = [event for event in store.events(0) if event["type"] == "release.published"]
    assert publications[-1]["payload"]["replace_ids"] == [{"unknown_id": first["template_id"], **mapping}]
    store.ingest(decode_icon(encoded_image(99)))
    temporary_revision = publisher.run_once()
    assert temporary_revision > mapped_revision
    publications = [event for event in store.events(0) if event["type"] == "release.published"]
    assert publications[-1]["payload"]["replace_ids"] == []
    manifest = verify(
        json.loads((config.data_dir / "public" / "manifest.json").read_bytes()),
        publisher.key.public_key().public_bytes_raw(),
    )
    assert len(manifest["replace_ids"]) == 1
    assert "image_base64" not in json.dumps(store.events(0), default=str)
