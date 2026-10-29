"""Immutable release snapshots, resumable deployment, then transactional activation."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
from sqlalchemy import select

from .config import Config
from .imaging import THRESHOLDS
from .models import Mapping, MappingDraft, PublishRequest, Release, State, TemplateIdentity
from .signing import envelope, load_key
from .store import Store, emit, fields, json_bytes, lock, snapshot, utcnow


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def template_key(entry):
    return entry["identity_id"] + ":" + entry["icon_sha256"]


def delta(before: dict, after: dict, revision: int, from_revision: int) -> dict:
    old = {template_key(entry): entry for entry in before.get("templates", [])}
    new = {template_key(entry): entry for entry in after["templates"]}
    # A mapped template changes its ID in place. replace_ids is for historical local records.
    return {
        "revision": revision,
        "from_revision": from_revision,
        "upserts": [value for key, value in new.items() if old.get(key) != value],
        "deletions": [key for key in old if key not in new],
        "replace_ids": after["mappings"],
    }


class Publisher:
    def __init__(self, store: Store, config: Config, deploy=None):
        self.store = store
        self.config = config
        self.key = load_key(config.data_dir)
        self.deploy = deploy or self.deploy_cloudflare

    def run_once(self):
        # Dedicated ORM row lock: only the publisher coordinates here, not upload transactions.
        with self.store.session.begin() as guard:
            if lock(guard, 2, skip_locked=True) is None:
                return None
            release = self.prepare()
            if release is None:
                return None
            try:
                distribution = self.render(release)
                if self.config.deploy_mode == "cloudflare":
                    self.deploy(distribution, release.revision)
                self.commit_local(distribution, release.revision)
                self.activate(release)
                return release.revision
            except Exception:
                with self.store.session.begin() as session:
                    row = session.get(Release, release.revision)
                    if row.status != "deployed":
                        first_failure = row.status != "failed"
                        row.status = "failed"
                        row.error = "Deployment failed; previous revision remains active. Retry is automatic."
                        if first_failure:
                            emit(
                                session,
                                "release.failed",
                                self.current_revision(session),
                                {"failed_revision": row.revision},
                            )
                raise

    @staticmethod
    def current_revision(session):
        return session.get(State, 1).revision

    def prepare(self):
        with self.store.session.begin() as session:
            lock(session, 1)
            # A failed or interrupted deployment is resumed with the same frozen payload and revision.
            pending = session.scalar(
                select(Release).where(Release.status != "deployed").order_by(Release.revision).limit(1)
            )
            if pending:
                return pending
            request = session.scalar(
                select(PublishRequest)
                .where(PublishRequest.processed.is_(False))
                .order_by(PublishRequest.id)
                .limit(1)
            )
            state = session.get(State, 1)
            if not request and state.generation <= state.published_generation:
                return None
            payload = snapshot(session, request.mappings if request else [])
            payload["created_at"] = utcnow().isoformat()
            payload["match_thresholds"] = dict(THRESHOLDS)
            release = Release(
                status="building",
                generation=state.generation,
                note=request.note if request else "Automatic temporary template publication",
                payload=payload,
                request_id=request.id if request else None,
            )
            session.add(release)
            if request:
                request.processed = True
            session.flush()
            return release

    def render(self, release: Release):
        root = self.config.data_dir / "builds" / str(release.revision)
        committed = self.config.data_dir / "public"
        if not root.exists() and committed.exists():
            shutil.copytree(committed, root, copy_function=os.link)
        directory = root / "releases" / str(release.revision)
        payload = release.payload
        entries = []
        for source in payload["templates"]:
            entry = dict(source)
            png = base64.b64decode(entry["image_base64"])
            file_hash = hashlib.sha256(png).hexdigest()
            relative = f"icons/{file_hash}.png"
            atomic_write(directory / relative, png)
            entry.update(
                sha256=file_hash, url=f"{self.config.public_base}/releases/{release.revision}/{relative}"
            )
            entries.append(entry)
        manifest = {
            "schema_version": 1,
            "revision": release.revision,
            "created_at": payload["created_at"],
            "templates": entries,
            "replace_ids": payload["mappings"],
            "match_thresholds": payload["match_thresholds"],
            "mappings_url": f"{self.config.public_base}/releases/{release.revision}/mappings.json",
        }
        manifest_data = json_bytes(envelope(manifest, self.key))
        atomic_write(directory / "manifest.json", manifest_data)
        atomic_write(
            directory / "mappings.json",
            json_bytes(
                envelope({"revision": release.revision, "replace_ids": payload["mappings"]}, self.key)
            ),
        )
        with self.store.session() as session:
            previous = session.scalars(
                select(Release)
                .where(Release.status == "deployed")
                .order_by(Release.revision.desc())
                .limit(50)
            ).all()
        for prior in [None, *previous]:
            from_revision = prior.revision if prior else 0
            change = delta(prior.payload if prior else {}, payload, release.revision, from_revision)
            atomic_write(
                directory / "deltas" / f"{from_revision}.json", json_bytes(envelope(change, self.key))
            )
        latest = {
            "revision": release.revision,
            "manifest_url": f"{self.config.public_base}/releases/{release.revision}/manifest.json",
            "manifest_sha256": hashlib.sha256(manifest_data).hexdigest(),
            "delta_base": f"{self.config.public_base}/releases/{release.revision}/deltas/",
        }
        # A deployment contains the pointer and all immutable resources in one Worker version.
        atomic_write(root / "latest.json", json_bytes(envelope(latest, self.key)))
        atomic_write(root / "manifest.json", (directory / "manifest.json").read_bytes())
        atomic_write(root / "mappings.json", (directory / "mappings.json").read_bytes())
        atomic_write(
            root / "_headers",
            b"/*\n  Access-Control-Allow-Origin: *\n  X-Content-Type-Options: nosniff\n/releases/*\n  Cache-Control: public, max-age=31536000, immutable\n/latest.json\n  Cache-Control: public, max-age=0, must-revalidate\n",
        )
        return root

    def commit_local(self, distribution: Path, revision: int):
        committed = self.config.data_dir / "public"
        target = committed / "releases" / str(revision)
        shutil.copytree(distribution / "releases" / str(revision), target, dirs_exist_ok=True)
        for name in ("manifest.json", "mappings.json", "_headers", "latest.json"):
            atomic_write(committed / name, (distribution / name).read_bytes())

    def deploy_cloudflare(self, directory: Path, revision: int):
        config_path = self.config.data_dir / "wrangler.generated.json"
        config = {
            "name": self.config.worker_name,
            "compatibility_date": "2026-10-28",
            "assets": {"directory": str(directory.resolve()), "html_handling": "none"},
            "workers_dev": False,
        }
        domain = os.getenv("CF_WORKER_DOMAIN", "")
        if domain:
            config["routes"] = [{"pattern": domain, "custom_domain": True}]
        atomic_write(config_path, json_bytes(config))
        result = subprocess.run(
            [
                "node",
                str(Path(self.config.wrangler).resolve()),
                "deploy",
                "--config",
                str(config_path.resolve()),
            ],
            timeout=180,
            cwd=self.config.data_dir,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Wrangler deployment failed with exit code {result.returncode}")
        expected = json.loads((directory / "latest.json").read_bytes())
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = client.get(self.config.public_base + "/latest.json")
            response.raise_for_status()
            if response.json() != expected:
                raise RuntimeError("Worker publication verification failed")

    def activate(self, release: Release):
        with self.store.session.begin() as session:
            lock(session, 1)
            row = session.get(Release, release.revision)
            if row.status == "deployed":
                return
            for mapping in row.payload["approved"]:
                session.merge(Mapping(**mapping, revision=row.revision))
                identity = session.get(TemplateIdentity, mapping["unknown_id"])
                identity.canonical_id, identity.name, identity.tag = (
                    mapping["item_id"],
                    mapping["name"],
                    mapping["tag"],
                )
                identity.kind = "formal"
                draft = session.get(MappingDraft, mapping["unknown_id"])
                if draft and fields(draft, "unknown_id", "item_id", "name", "tag") == mapping:
                    session.delete(draft)
            state = session.get(State, 1)
            state.revision = row.revision
            state.published_generation = row.generation
            row.status, row.error, row.deployed_at = "deployed", "", utcnow()
            emit(
                session,
                "release.published",
                row.revision,
                {
                    "revision": row.revision,
                    "replace_ids": row.payload["mappings"],
                    "template_count": len(row.payload["templates"]),
                    "manifest_url": f"{self.config.public_base}/releases/{row.revision}/manifest.json",
                },
            )
