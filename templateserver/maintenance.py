"""Periodic template consolidation without deleting source identities or evidence."""

import logging

from sqlalchemy import select

from .imaging import similarity
from .models import IconImage, MappingDraft, TemplateIdentity, State
from .store import fields, lock

logger = logging.getLogger(__name__)


class Maintenance:
    def __init__(self, store, threshold=0.85):
        self.store, self.threshold = store, threshold

    def run_once(self, stop=None):
        # Pixel comparison must not hold the ingestion/publication coordination lock.
        with self.store.session() as session:
            rows = session.execute(
                select(IconImage, TemplateIdentity)
                .join(TemplateIdentity, IconImage.identity_id == TemplateIdentity.id)
                .where(TemplateIdentity.discarded.is_(False))
                .order_by(TemplateIdentity.created_at, TemplateIdentity.id, IconImage.sha256)
            ).all()
            parent = {identity.canonical_id: identity.canonical_id for _, identity in rows}
            formal = {
                identity.canonical_id: {identity.canonical_id} if identity.kind == "formal" else set()
                for _, identity in rows
            }

        def root(key):
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        for index, (left, left_identity) in enumerate(rows):
            if stop is not None and stop.is_set():
                return {"merged": 0, "groups": 0, "stopped": True}
            for right, right_identity in rows[index + 1:]:
                a, b = root(left_identity.canonical_id), root(right_identity.canonical_id)
                if a == b or len(formal[a] | formal[b]) > 1:
                    continue
                if similarity(left.pixels, right.pixels, left.stddev, right.stddev) >= self.threshold:
                    parent[b] = a
                    formal[a] |= formal[b]

        components = {}
        for key in parent:
            components.setdefault(root(key), set()).add(key)
        components = [group for group in components.values() if len(group) > 1]
        if not components:
            return {"merged": 0, "groups": 0}

        merged, groups = 0, 0
        with self.store.session.begin() as session:
            lock(session, 1)
            identities = list(session.scalars(
                select(TemplateIdentity).where(TemplateIdentity.discarded.is_(False))
                .order_by(TemplateIdentity.created_at, TemplateIdentity.id)
            ))
            drafts = {draft.unknown_id: draft for draft in session.scalars(select(MappingDraft))}
            for component in components:
                members = [identity for identity in identities if identity.canonical_id in component]
                canonical = {identity.canonical_id for identity in members}
                known = {identity.canonical_id for identity in members if identity.kind == "formal"}
                pending = [drafts[identity.id] for identity in members if identity.id in drafts]
                draft_values = {(draft.item_id, draft.name, draft.tag) for draft in pending}
                if len(canonical) < 2 or len(known) > 1 or len(draft_values) > 1:
                    continue
                if known and pending and pending[0].item_id not in known:
                    continue
                target = next(iter(known)) if known else members[0].canonical_id
                representative = next(identity for identity in members if identity.canonical_id == target)
                for identity in members:
                    identity.canonical_id = target
                    identity.kind = "formal" if known else "temporary"
                    identity.name, identity.tag = representative.name, representative.tag
                    if pending and identity.id.startswith("U-"):
                        session.merge(MappingDraft(
                            unknown_id=identity.id, updated_at=pending[0].updated_at,
                            **fields(pending[0], "item_id", "name", "tag"),
                        ))
                merged += len(canonical) - 1
                groups += 1
            if merged:
                session.get(State, 1).generation += 1
        result = {"merged": merged, "groups": groups}
        logger.info("Automatic template merge: %s", result)
        return result
