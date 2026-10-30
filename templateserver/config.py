import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Config:
    database_url: str
    admin_token: str
    event_token: str
    admin_origins: tuple[str, ...] = ("https://api.azurpilot.com", "https://stats.azurpilot.com")
    public_base: str = "http://127.0.0.1:8787"
    main_event_url: str = ""
    data_dir: Path = Path("data")
    deploy_mode: str = "local"
    worker_name: str = "azurpilot-templates"
    wrangler: str = "node_modules/wrangler/bin/wrangler.js"
    auto_publish_seconds: float = 3

    @classmethod
    def from_env(cls):
        cfg = cls(
            database_url=os.environ["DATABASE_URL"],
            admin_token=os.getenv("TEMPLATE_ADMIN_TOKEN", ""),
            event_token=os.getenv("TEMPLATE_EVENT_TOKEN", ""),
            admin_origins=tuple(
                origin.strip().rstrip("/")
                for origin in os.getenv(
                    "TEMPLATE_ADMIN_ORIGINS", "https://api.azurpilot.com,https://stats.azurpilot.com"
                ).split(",")
                if origin.strip()
            ),
            public_base=os.getenv("TEMPLATE_PUBLIC_BASE", "http://127.0.0.1:8787").rstrip("/"),
            main_event_url=os.getenv("MAIN_EVENT_URL", ""),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            deploy_mode=os.getenv("DEPLOY_MODE", "local"),
            worker_name=os.getenv("CF_WORKER_NAME", "azurpilot-templates"),
            wrangler=os.getenv("WRANGLER_SCRIPT", "node_modules/wrangler/bin/wrangler.js"),
            auto_publish_seconds=float(os.getenv("AUTO_PUBLISH_SECONDS", "3")),
        )
        if cfg.deploy_mode not in {"local", "cloudflare"}:
            raise ValueError("DEPLOY_MODE must be local or cloudflare")
        if cfg.auto_publish_seconds < 0.1:
            raise ValueError("AUTO_PUBLISH_SECONDS must be >= 0.1")
        for origin in cfg.admin_origins:
            parsed = urlparse(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.path
                or parsed.query
                or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
            ):
                raise ValueError("Admin origins must be HTTPS origins (loopback HTTP is allowed)")
        for url in [cfg.public_base, cfg.main_event_url]:
            if not url:
                continue
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
                raise ValueError("Invalid service URL")
            if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Non-loopback service URLs must use HTTPS")
        if cfg.deploy_mode == "cloudflare":
            for key in ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"]:
                if not os.getenv(key):
                    raise ValueError(f"{key} is required in cloudflare mode")
        return cfg
