"""Persistence models; repositories use SQLAlchemy expressions, never SQL strings."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Identity,
    Integer,
    LargeBinary,
    String,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class State(Base):
    __tablename__ = "template_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, default=0)
    published_generation: Mapped[int] = mapped_column(BigInteger, default=-1)
    revision: Mapped[int] = mapped_column(BigInteger, default=0)


class CoordinationLock(Base):
    """Rows 1/2/3 serialize identities/publication/delivery across instances."""

    __tablename__ = "coordination_locks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class TemplateIdentity(Base):
    __tablename__ = "identities"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    tag: Mapped[str] = mapped_column(String(32), default="")
    kind: Mapped[str] = mapped_column(Enum("temporary", "formal", name="template_kind"))
    discarded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), index=True)
    hits: Mapped[int] = mapped_column(BigInteger, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IconImage(Base):
    __tablename__ = "icons"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), index=True)
    png: Mapped[bytes] = mapped_column(LargeBinary)
    pixels: Mapped[bytes] = mapped_column(LargeBinary)
    phash: Mapped[str] = mapped_column(String(16))
    mean: Mapped[list[float]] = mapped_column(ARRAY(Float))
    stddev: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint(func.octet_length(pixels) == 7203, name="icon_pixel_length"),)


class Screenshot(Base):
    __tablename__ = "screenshots"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    png: Mapped[bytes] = mapped_column(LargeBinary)
    scene: Mapped[str] = mapped_column(String(64))
    client: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScreenshotReference(Base):
    __tablename__ = "screenshot_references"
    screenshot_sha256: Mapped[str] = mapped_column(ForeignKey("screenshots.sha256"), primary_key=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), primary_key=True, index=True)
    x1: Mapped[int] = mapped_column(Integer, primary_key=True)
    y1: Mapped[int] = mapped_column(Integer, primary_key=True)
    x2: Mapped[int] = mapped_column(Integer, primary_key=True)
    y2: Mapped[int] = mapped_column(Integer, primary_key=True)


class MappingDraft(Base):
    __tablename__ = "mapping_drafts"
    unknown_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    tag: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PublishRequest(Base):
    """An administrator approves a snapshot, never future draft edits."""

    __tablename__ = "publish_requests"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    note: Mapped[str] = mapped_column(String(500))
    mappings: Mapped[list] = mapped_column(JSONB)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Release(Base):
    __tablename__ = "releases"
    revision: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    status: Mapped[str] = mapped_column(Enum("building", "deployed", "failed", name="release_status"))
    generation: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict] = mapped_column(JSONB)
    request_id: Mapped[int | None] = mapped_column(ForeignKey("publish_requests.id"))
    error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Mapping(Base):
    __tablename__ = "mappings"
    unknown_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    tag: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(ForeignKey("releases.revision"))


class OutboxEvent(Base):
    __tablename__ = "outbox"
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4, unique=True)
    event_type: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
