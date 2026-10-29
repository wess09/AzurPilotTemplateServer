"""Generate an empty local release for Worker validation; never deploys externally."""

from pathlib import Path

from templateserver.config import Config
from templateserver.publishing import Publisher
from templateserver.store import Store

if __name__ == "__main__":
    config = Config.from_env()
    store = Store(config.database_url)
    store.open()
    Publisher(store, config).run_once()
    assert Path(config.data_dir / "public" / "latest.json").exists()
    store.close()
