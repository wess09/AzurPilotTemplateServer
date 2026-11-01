import base64
import json
import secrets
from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, load_only, sessionmaker

from .imaging import THRESHOLDS, Icon, match_icon
from .models import (
    CoordinationLock,
    IconImage,
    ItemDefinition,
    Mapping,
    MappingDraft,
    OutboxEvent,
    PublishRequest,
    Release,
    Screenshot,
    ScreenshotReference,
    State,
    TemplateIdentity,
)


def utcnow():
    return datetime.now(UTC)


def fields(row, *names):
    return {name: getattr(row, name) for name in names}


def draft_fields(row):
    return {**fields(row, "unknown_id", "item_id", "name", "tag"), "metadata": row.item_metadata}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 409):
        self.message = message
        self.status = status


def lock(session: Session, number: int, *, skip_locked: bool = False):
    return session.scalar(
        select(CoordinationLock).where(CoordinationLock.id == number).with_for_update(skip_locked=skip_locked)
    )


def emit(session: Session, event_type: str, revision: int, payload: dict):
    session.add(OutboxEvent(event_type=event_type, revision=revision, payload=payload))


def database_url(url: str):
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("The template service requires PostgreSQL")
    return url


class Store:
    def __init__(self, url: str):
        self.engine = create_engine(
            database_url(url), pool_pre_ping=True, pool_size=8, max_overflow=8, hide_parameters=True
        )
        self.session = sessionmaker(self.engine, expire_on_commit=False)
        self.match_thresholds = dict(THRESHOLDS)

    def open(self):
        with self.session() as session:
            locks = set(session.scalars(select(CoordinationLock.id)))
            if session.get(State, 1) is None or not {1, 2, 3}.issubset(locks):
                raise RuntimeError("Run alembic upgrade head before starting the service")

    def close(self):
        self.engine.dispose()

    def ingest(self, icon: Icon, formal: dict | None = None) -> dict:
        with self.session.begin() as session:
            lock(session, 1)
            image = session.get(IconImage, icon.sha256)
            identity = session.get(TemplateIdentity, image.identity_id) if image else None
            if formal:
                if identity and identity.canonical_id != formal["item_id"]:
                    raise DomainError(f"Icon already belongs to {identity.id}; create a mapping instead")
                if not identity:
                    identity = session.get(TemplateIdentity, formal["item_id"])
                    if identity is None:
                        identity = session.scalar(
                            select(TemplateIdentity).where(TemplateIdentity.canonical_id == formal["item_id"])
                            .order_by(TemplateIdentity.discarded, TemplateIdentity.created_at).limit(1)
                        )
            elif not identity:
                rows = session.execute(
                    select(IconImage, TemplateIdentity).join(
                        TemplateIdentity, TemplateIdentity.id == IconImage.identity_id
                    )
                ).all()
                candidates = [
                    {
                        **fields(target, "id", "canonical_id", "kind", "discarded"),
                        **fields(sample, "pixels", "phash", "mean", "stddev"),
                    }
                    for sample, target in rows
                ]
                matched = match_icon(icon, [row for row in candidates if not row["discarded"]], self.match_thresholds)
                if matched is None:
                    matched = match_icon(icon, [{**row, "kind": "temporary"} for row in candidates if row["discarded"]], self.match_thresholds)
                identity = session.get(TemplateIdentity, matched["id"]) if matched else None
            if identity and identity.discarded:
                if formal:
                    raise DomainError("模板已丢弃，请先恢复再导入")
                state = session.get(State, 1)
                # Keep the acknowledgement compatible with existing clients without registering
                # another identity or admitting this image into the recognition catalog.
                return {
                    "match": "formal" if identity.kind == "formal" else "cluster",
                    "template_id": identity.canonical_id,
                    "identity_id": identity.id,
                    "item_id": identity.canonical_id if identity.kind == "formal" else None,
                    "name": identity.name, "tag": identity.tag, "discarded": True,
                    "revision": state.revision,
                    "publication_pending": state.generation > state.published_generation,
                }
            created = identity is None
            if created:
                identity_id = formal["item_id"] if formal else "U-" + secrets.token_hex(6).upper()
                if not formal:
                    while session.get(TemplateIdentity, identity_id):
                        identity_id = "U-" + secrets.token_hex(6).upper()
                identity = TemplateIdentity(
                    id=identity_id,
                    canonical_id=identity_id,
                    name=formal["name"] if formal else "",
                    tag=formal.get("tag", "") if formal else "",
                    kind="formal" if formal else "temporary",
                )
                session.add(identity)
                session.flush()
            else:
                identity.hits += 1
                identity.last_seen = utcnow()
            count = session.scalar(
                select(func.count()).select_from(IconImage).where(IconImage.identity_id == identity.id)
            )
            state = session.get(State, 1)
            if formal:
                if identity.name != formal["name"] or identity.tag != formal.get("tag", ""):
                    identity.name, identity.tag = formal["name"], formal.get("tag", "")
                    state.generation += 1
                if formal.get("metadata") is not None:
                    definition = session.get(ItemDefinition, formal["item_id"])
                    if definition is None or definition.name != formal["name"] or definition.item_metadata != formal["metadata"]:
                        session.merge(ItemDefinition(item_id=formal["item_id"], name=formal["name"], item_metadata=formal["metadata"]))
                        state.generation += 1
            if image is None and (formal is not None or count < 8):
                session.add(
                    IconImage(
                        identity_id=identity.id,
                        **fields(icon, "sha256", "png", "pixels", "phash", "mean", "stddev"),
                    )
                )
                state.generation += 1
            if created:
                emit(
                    session,
                    "template.registered",
                    state.revision,
                    {
                        "template_id": identity.canonical_id,
                        "kind": identity.kind,
                        "generation": state.generation,
                    },
                )
            return {
                "match": "formal" if identity.kind == "formal" else ("new" if created else "cluster"),
                "template_id": identity.canonical_id,
                "identity_id": identity.id,
                "item_id": identity.canonical_id if identity.kind == "formal" else None,
                "name": identity.name,
                "tag": identity.tag,
                "revision": state.revision,
                "publication_pending": state.generation > state.published_generation,
            }

    def save_item(self, body: dict):
        with self.session.begin() as session:
            lock(session, 1)
            state = session.get(State, 1)
            definition = session.get(ItemDefinition, body["item_id"])
            if definition is None or definition.name != body["name"] or definition.item_metadata != body["metadata"]:
                session.merge(ItemDefinition(item_id=body["item_id"], name=body["name"], item_metadata=body["metadata"]))
                for identity in session.scalars(select(TemplateIdentity).where(TemplateIdentity.canonical_id == body["item_id"])):
                    identity.name = body["name"]
                state.generation += 1
            return {"saved": True, "publication_pending": state.generation > state.published_generation}

    def save_mapping(self, unknown_id: str, body: dict):
        with self.session.begin() as session:
            lock(session, 1)
            identity = session.get(TemplateIdentity, unknown_id)
            if not unknown_id.startswith("U-") or not identity:
                raise DomainError("Unknown template ID does not exist", 404)
            if identity.discarded:
                raise DomainError("模板已丢弃，请先恢复")
            members = session.scalars(
                select(TemplateIdentity).where(TemplateIdentity.canonical_id == identity.canonical_id)
            )
            for member in members:
                if member.id.startswith("U-"):
                    session.merge(MappingDraft(
                        unknown_id=member.id, updated_at=utcnow(),
                        item_metadata=body.get("metadata"), **{key: value for key, value in body.items() if key != "metadata"},
                    ))

    def set_discarded(self, identity_id: str, discarded: bool):
        with self.session.begin() as session:
            lock(session, 1)
            identity = session.get(TemplateIdentity, identity_id)
            if identity is None:
                raise DomainError("Template not found", 404)
            members = list(session.scalars(
                select(TemplateIdentity).where(TemplateIdentity.canonical_id == identity.canonical_id)
            ))
            changed = False
            for member in members:
                changed |= member.discarded != discarded
                member.discarded = discarded
                if discarded:
                    draft = session.get(MappingDraft, member.id)
                    if draft:
                        session.delete(draft)
            if changed:
                session.get(State, 1).generation += 1
            return {"discarded": discarded, "identities": len(members), "published": False}

    def save_screenshot(self, digest: str, png: bytes, scene: str, client: str, references: list[dict]):
        with self.session.begin() as session:
            lock(session, 1)
            for reference in references:
                if session.get(TemplateIdentity, reference["identity_id"]) is None:
                    raise DomainError("Register every template before uploading its screenshot", 404)
            if session.get(Screenshot, digest) is None:
                session.add(Screenshot(sha256=digest, png=png, scene=scene, client=client))
                session.flush()
            for reference in references:
                key = (digest, reference["identity_id"], *reference["bounds"])
                if session.get(ScreenshotReference, key) is None:
                    session.add(
                        ScreenshotReference(
                            **dict(
                                zip(
                                    ("screenshot_sha256", "identity_id", "x1", "y1", "x2", "y2"),
                                    key,
                                    strict=True,
                                )
                            )
                        )
                    )
                    session.flush()
        # Evidence is private administration data, never a release asset or main-server event.
        return {"sha256": digest, "stored": True}

    def screenshots(self, identity_id: str, limit: int, offset: int):
        with self.session() as session:
            identity = session.get(TemplateIdentity, identity_id)
            if identity is None:
                raise DomainError("Template not found", 404)
            members = select(TemplateIdentity.id).where(TemplateIdentity.canonical_id == identity.canonical_id)
            rows = session.execute(
                select(
                    Screenshot.sha256,
                    Screenshot.scene,
                    Screenshot.created_at,
                    ScreenshotReference.x1,
                    ScreenshotReference.y1,
                    ScreenshotReference.x2,
                    ScreenshotReference.y2,
                )
                .join(ScreenshotReference, ScreenshotReference.screenshot_sha256 == Screenshot.sha256)
                .where(ScreenshotReference.identity_id.in_(members))
                .distinct()
                .order_by(
                    Screenshot.created_at.desc(),
                    Screenshot.sha256,
                    ScreenshotReference.x1,
                    ScreenshotReference.y1,
                )
                .limit(limit)
                .offset(offset)
            )
            return [
                {
                    "sha256": row.sha256,
                    "scene": row.scene,
                    "created_at": row.created_at,
                    "width": 1280,
                    "height": 720,
                    "bounds": [row.x1, row.y1, row.x2, row.y2],
                }
                for row in rows
            ]

    def screenshot_chunk(self, digest: str, offset: int, length: int):
        with self.session() as session:
            png = session.scalar(select(Screenshot.png).where(Screenshot.sha256 == digest))
            if png is None:
                raise DomainError("Screenshot not found", 404)
            if offset >= len(png):
                raise DomainError("Screenshot offset is outside the image", 422)
            return {
                "data_base64": base64.b64encode(png[offset : offset + length]).decode(),
                "offset": offset,
                "total_size": len(png),
                "media_type": "image/png",
            }

    def request_publish(self, note: str):
        with self.session.begin() as session:
            lock(session, 1)
            drafts = session.scalars(select(MappingDraft).order_by(MappingDraft.unknown_id))
            request = PublishRequest(
                note=note, mappings=[draft_fields(d) for d in drafts]
            )
            session.add(request)
            session.flush()
            return request.id

    def status(self):
        with self.session() as session:
            state = fields(session.get(State, 1), "generation", "published_generation", "revision")
            state["templates"] = [
                dict(row._mapping)
                for row in session.execute(
                    select(TemplateIdentity.kind, func.count(func.distinct(TemplateIdentity.canonical_id)).label("count"))
                    .where(TemplateIdentity.discarded.is_(False))
                    .group_by(TemplateIdentity.kind)
                )
            ]
            state["drafts"] = session.scalar(select(func.count()).select_from(MappingDraft))
            state["last_draft_updated_at"] = session.scalar(select(func.max(MappingDraft.updated_at)))
            state["last_activity"] = session.scalar(select(func.max(TemplateIdentity.last_seen)))
            state["screenshots"] = session.scalar(select(func.count()).select_from(Screenshot))
            state["last_screenshot_at"] = session.scalar(select(func.max(Screenshot.created_at)))
            state["total_hits"] = session.scalar(select(func.coalesce(func.sum(TemplateIdentity.hits), 0)))
            state["pending_events"] = session.scalar(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.acknowledged_at.is_(None))
            )
            state["pending_publications"] = session.scalar(
                select(func.count()).select_from(PublishRequest).where(PublishRequest.processed.is_(False))
            )
            event = session.scalar(select(OutboxEvent).order_by(OutboxEvent.sequence.desc()).limit(1))
            state["last_delivery"] = (
                fields(event, "sequence", "acknowledged_at", "last_error", "attempts") if event else None
            )
            release = session.scalar(
                select(Release).options(load_only(
                    Release.revision, Release.status, Release.error, Release.created_at, Release.deployed_at,
                )).order_by(Release.revision.desc()).limit(1)
            )
            state["release"] = (
                fields(release, "revision", "status", "error", "created_at", "deployed_at")
                if release
                else None
            )
            return state

    def templates(self, kind: str | None, limit: int, offset: int, discarded: bool = False):
        with self.session() as session:
            statement = select(TemplateIdentity.canonical_id).where(TemplateIdentity.discarded == discarded).group_by(TemplateIdentity.canonical_id).order_by(
                func.max(TemplateIdentity.created_at).desc(), TemplateIdentity.canonical_id
            )
            if kind:
                statement = statement.where(TemplateIdentity.kind == kind)
            result = []
            canonical_ids = list(session.scalars(statement.limit(limit).offset(offset)))
            members = list(session.scalars(
                select(TemplateIdentity).where(TemplateIdentity.canonical_id.in_(canonical_ids))
                .order_by(TemplateIdentity.created_at, TemplateIdentity.id)
            ))
            for canonical_id in canonical_ids:
                group = [member for member in members if member.canonical_id == canonical_id]
                row = next((member for member in group if member.id == canonical_id), group[0])
                member_ids = [member.id for member in group]
                item = fields(row, "id", "canonical_id", "name", "tag", "kind", "created_at", "discarded")
                item["hits"] = sum(member.hits for member in group)
                item["last_seen"] = max(member.last_seen for member in group)
                item["aliases"] = [member.id for member in group if member.id != row.id]
                definition = session.get(ItemDefinition, row.canonical_id)
                item["metadata"] = definition.item_metadata if definition else None
                draft = session.scalar(select(MappingDraft).where(MappingDraft.unknown_id.in_(member_ids)).limit(1))
                item["draft"] = {**fields(draft, "item_id", "name", "tag"), "metadata": draft.item_metadata} if draft else None
                image = session.scalar(
                    select(IconImage).where(IconImage.identity_id.in_(member_ids))
                    .order_by(IconImage.created_at, IconImage.sha256).limit(1)
                )
                item["icon_sha256"] = image.sha256 if image else None
                item["image_base64"] = base64.b64encode(image.png).decode() if image else None
                item["screenshot_count"] = session.scalar(
                    select(func.count(func.distinct(ScreenshotReference.screenshot_sha256)))
                    .where(ScreenshotReference.identity_id.in_(member_ids))
                )
                result.append(item)
            return result

    def icon_png(self, sha256: str):
        with self.session() as session:
            return session.scalar(select(IconImage.png).where(IconImage.sha256 == sha256))

    def items(self, limit: int, offset: int):
        with self.session() as session:
            return [
                {"item_id": row.item_id, "name": row.name, "tag": "", "metadata": row.item_metadata}
                for row in session.scalars(select(ItemDefinition).order_by(ItemDefinition.item_id).limit(limit).offset(offset))
            ]

    def releases(self):
        with self.session() as session:
            return [
                fields(row, "revision", "status", "request_id", "note", "error", "created_at", "deployed_at")
                for row in session.scalars(
                    select(Release).options(load_only(
                        Release.revision, Release.status, Release.request_id, Release.note,
                        Release.error, Release.created_at, Release.deployed_at,
                    )).order_by(Release.revision.desc()).limit(100)
                )
            ]

    def events(self, after: int):
        with self.session() as session:
            result = []
            for row in session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.sequence > after)
                .order_by(OutboxEvent.sequence)
                .limit(100)
            ):
                event = fields(row, "sequence", "event_id", "revision", "payload", "created_at")
                event["type"] = row.event_type
                result.append(event)
            return result


def snapshot(session: Session, approved: list[dict]) -> dict:
    items = {row.item_id: {"id": row.item_id, "name": row.name, **row.item_metadata} for row in session.scalars(select(ItemDefinition))}
    identities = {row.id: row for row in session.scalars(select(TemplateIdentity))}
    overrides = {}
    approved_items = {}
    for row in approved:
        identity = identities[row["unknown_id"]]
        if identity.discarded:
            continue
        old = overrides.get(identity.canonical_id)
        if old and not fields_equal(old, row):
            raise DomainError("Merged templates have conflicting mapping drafts")
        overrides[identity.canonical_id] = row
        if row.get("metadata"):
            definition = {"id": row["item_id"], "name": row["name"], **row["metadata"]}
            if row["item_id"] in approved_items and approved_items[row["item_id"]] != definition:
                raise DomainError("同一物品的映射草稿属性不一致，请统一后再发布")
            items[row["item_id"]] = definition
            approved_items[row["item_id"]] = definition
    mappings, expanded = {}, []
    for identity in identities.values():
        override = overrides.get(identity.canonical_id)
        if not identity.id.startswith("U-"):
            continue
        target = override["item_id"] if override else identity.canonical_id
        if target == identity.id:
            continue
        mapping = {
            "unknown_id": identity.id,
            "item_id": target,
            "name": override["name"] if override else identity.name or target,
            "tag": override["tag"] if override else identity.tag,
        }
        mappings[identity.id] = mapping
        if override:
            expanded.append(mapping)
    templates = []
    rows = session.execute(
        select(IconImage, TemplateIdentity)
        .join(TemplateIdentity, IconImage.identity_id == TemplateIdentity.id)
        .where(TemplateIdentity.discarded.is_(False))
        .order_by(TemplateIdentity.id, IconImage.sha256)
    )
    for icon, identity in rows:
        mapping = mappings.get(identity.id)
        templates.append(
            {
                "identity_id": identity.id,
                "template_id": mapping["item_id"] if mapping else identity.canonical_id,
                "kind": "formal" if mapping and not mapping["item_id"].startswith("U-") else identity.kind,
                "name": mapping["name"] if mapping else identity.name,
                "tag": mapping["tag"] if mapping else identity.tag,
                "icon_sha256": icon.sha256,
                "image_base64": base64.b64encode(icon.png).decode(),
            }
        )
    return {
        "templates": templates,
        "mappings": sorted(mappings.values(), key=lambda row: row["unknown_id"]),
        "approved": expanded,
        "items": sorted(items.values(), key=lambda row: row["id"]),
        "approved_items": list(approved_items.values()),
    }


def fields_equal(left, right):
    return all(left[key] == right[key] for key in ("item_id", "name", "tag")) and left.get("metadata") == right.get("metadata")


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
