"""Immutable release snapshots, resumable deployment, then transactional activation."""

import base64
import hashlib
import json
import logging
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


logger = logging.getLogger(__name__)
ASSET_FILE_LIMIT = 20_000
ASSET_SIZE_LIMIT = 25 * 1024 * 1024
RELEASE_HISTORY_LIMIT = 50


class PublicationError(RuntimeError):
    """A deployment diagnostic that is safe to display in administration."""


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def remove_build_directory(path: Path, root: Path):
    if path.exists():
        resolved, boundary = path.resolve(), root.resolve()
        if path.is_symlink() or not resolved.is_relative_to(boundary) or resolved == boundary:
            raise PublicationError("Invalid directory in the deployment build.")
        shutil.rmtree(path)


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
            except Exception as error:
                with self.store.session.begin() as session:
                    row = session.get(Release, release.revision)
                    if row.status != "deployed":
                        first_failure = row.status != "failed"
                        row.status = "failed"
                        row.error = (
                            str(error)
                            if isinstance(error, PublicationError)
                            else "Deployment failed; previous revision remains active. Retry is automatic."
                        )
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
            payload["match_thresholds"] = {**THRESHOLDS, "cluster": self.config.merge_similarity}
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
        directory = root / "releases" / str(release.revision)
        assets = root / "releases" / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        archive = self.config.data_dir / "public" / "releases" / "assets"
        if archive.exists():
            for icon in archive.glob("*.png"):
                if not (assets / icon.name).exists():
                    os.link(icon, assets / icon.name)
        payload = release.payload
        entries = []
        for source in payload["templates"]:
            entry = dict(source)
            png = base64.b64decode(entry.pop("image_base64"))
            file_hash = hashlib.sha256(png).hexdigest()
            relative = f"releases/assets/{file_hash}.png"
            if not (root / relative).exists():
                atomic_write(root / relative, png)
            entry.update(sha256=file_hash, url=f"{self.config.public_base}/{relative}")
            entries.append(entry)
        # Old failed builds stored a complete icon copy inside this revision.
        remove_build_directory(directory / "icons", root)
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
        public_entries = {template_key(entry): entry for entry in entries}

        def write_delta(before, from_revision):
            change = delta(before, payload, release.revision, from_revision)
            change["upserts"] = [public_entries[template_key(entry)] for entry in change["upserts"]]
            atomic_write(
                directory / "deltas" / f"{from_revision}.json", json_bytes(envelope(change, self.key))
            )

        write_delta({}, 0)
        with self.store.session() as session:
            previous = session.scalars(
                select(Release)
                .where(Release.status == "deployed")
                .order_by(Release.revision.desc())
                .limit(RELEASE_HISTORY_LIMIT)
                .execution_options(yield_per=1)
            )
            for prior in previous:
                write_delta(prior.payload, prior.revision)
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
        # Internal rewrites return 200; older clients intentionally refuse HTTP redirects.
        atomic_write(root / "_redirects", b"/releases/:revision/icons/:file /releases/assets/:file 200\n")
        atomic_write(
            root / "_headers",
            b"/*\n  Access-Control-Allow-Origin: *\n  X-Content-Type-Options: nosniff\n/releases/*\n  Cache-Control: public, max-age=31536000, immutable\n/latest.json\n  Cache-Control: public, max-age=0, must-revalidate\n",
        )
        self.retain_history(root, release.revision)
        return root

    def retain_history(self, root: Path, revision: int):
        """Stage recent releases within the asset budget; leave the local archive intact."""
        releases = root / "releases"
        archive = self.config.data_dir / "public" / "releases"
        current = releases / str(revision)
        assets = releases / "assets"
        count = sum(path.is_file() for path in current.rglob("*"))
        count += sum(path.is_file() for path in assets.rglob("*"))
        count += sum(path.is_file() for path in root.iterdir())
        sources = {}
        for parent in (archive, releases):
            if parent.exists():
                for child in parent.iterdir():
                    if child.is_dir() and not child.is_symlink() and child.name.isdecimal():
                        number = int(child.name)
                        if number < revision:
                            sources[number] = child
        retained = {revision}
        for number in sorted(sources, reverse=True)[:RELEASE_HISTORY_LIMIT]:
            source = sources[number]
            # Legacy signed manifests keep their original URLs. Their PNGs move into
            # the shared pool, and _redirects serves those URLs without extra copies.
            icons = {path.name: path for path in (source / "icons").glob("*.png")}
            missing = {name: path for name, path in icons.items() if not (assets / name).exists()}
            documents = sum(
                path.is_file() and path.relative_to(source).parts[0] != "icons"
                for path in source.rglob("*")
            )
            if count + documents + len(missing) > ASSET_FILE_LIMIT:
                break
            target = releases / str(number)
            if source != target:
                shutil.copytree(source, target, copy_function=os.link, ignore=shutil.ignore_patterns("icons"))
            for name, path in missing.items():
                os.link(path, assets / name)
            remove_build_directory(target / "icons", root)
            retained.add(number)
            count += documents + len(missing)
        # A resumed build may still contain the entire archive staged by older code.
        for child in releases.iterdir():
            if child.name.isdecimal() and int(child.name) not in retained:
                remove_build_directory(child, root)
        logger.info("Release %s staged with %s assets and %s retained releases", revision, count, len(retained))

    def commit_local(self, distribution: Path, revision: int):
        committed = self.config.data_dir / "public"
        assets = committed / "releases" / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        for icon in (distribution / "releases" / "assets").glob("*.png"):
            if not (assets / icon.name).exists():
                os.link(icon, assets / icon.name)
        target = committed / "releases" / str(revision)
        shutil.copytree(distribution / "releases" / str(revision), target, dirs_exist_ok=True)
        for name in ("manifest.json", "mappings.json", "_headers", "_redirects", "latest.json"):
            atomic_write(committed / name, (distribution / name).read_bytes())

    def deploy_cloudflare(self, directory: Path, revision: int):
        assets = [path for path in directory.rglob("*") if path.is_file()]
        if len(assets) > ASSET_FILE_LIMIT:
            raise PublicationError(
                f"Deployment contains {len(assets)} static assets; the limit is {ASSET_FILE_LIMIT}."
            )
        for path in assets:
            if path.stat().st_size > ASSET_SIZE_LIMIT:
                raise PublicationError(
                    f"Static asset {path.relative_to(directory)} exceeds the 25 MiB file limit."
                )
        config_path = self.config.data_dir / "wrangler.generated.json"
        config = {
            "name": self.config.worker_name,
            "compatibility_date": "2026-10-28",
            "assets": {"directory": str(directory.resolve()), "html_handling": "none"},
            "workers_dev": False,
        }
        domain = os.getenv("CF_WORKER_DOMAIN", "")
        if domain:
            zone = os.getenv("CF_WORKER_ZONE_NAME", "")
            config["routes"] = (
                [{"pattern": domain + "/*", "zone_name": zone}]
                if zone else [{"pattern": domain, "custom_domain": True}]
            )
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
            diagnostic = (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace")
            for secret in (
                self.config.admin_token,
                self.config.event_token,
                self.config.database_url,
                os.getenv("CLOUDFLARE_API_TOKEN", ""),
            ):
                if secret:
                    diagnostic = diagnostic.replace(secret, "[redacted]")
            logger.error("Wrangler deployment failed: %s", diagnostic[-8192:])
            if "100117" in diagnostic:
                raise PublicationError(
                    "域名已有 DNS 记录，与 Custom Domain 冲突；使用现有 Worker 路由时请配置 CF_WORKER_ZONE_NAME。"
                )
            raise PublicationError(
                f"Wrangler deployment failed (exit {result.returncode}); see template server logs. Retry is automatic."
            )
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
            state = session.get(State, 1)
            prior = session.get(Release, state.revision) if state.revision else None
            previous = {mapping["unknown_id"]: mapping for mapping in prior.payload["mappings"]} if prior else {}
            changed = [mapping for mapping in row.payload["mappings"] if previous.get(mapping["unknown_id"]) != mapping]
            for mapping in row.payload["mappings"]:
                session.merge(Mapping(**mapping, revision=row.revision))
            for mapping in row.payload["approved"]:
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
            state.revision = row.revision
            state.published_generation = row.generation
            row.status, row.error, row.deployed_at = "deployed", "", utcnow()
            emit(
                session,
                "release.published",
                row.revision,
                {
                    "revision": row.revision,
                    # Replicate changed aliases, including U-ID consolidation and later formalization.
                    "replace_ids": changed,
                    "template_count": len(row.payload["templates"]),
                    "manifest_url": f"{self.config.public_base}/releases/{row.revision}/manifest.json",
                },
            )
