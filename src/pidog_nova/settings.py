"""pidog-nova configuration.

All settings come from environment variables.  Persisted runtime config
(currently the upstream PiDog IP/port) is dual-read from the Nova Object
Store and a local JSON file.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from pidog_nova import config_store

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings — populated from env vars + persistent config store."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── API ───────────────────────────────────────────────────────────
    api_title: str = "Pidog Nova Gateway"
    api_version: str = "1.0.0"  # Keep in sync with pyproject.toml [project].version

    # ── Server ────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False
    log_level: str = "INFO"

    # ── Robot ─────────────────────────────────────────────────────────
    robot_model: str = "pidog"
    robot_id: str = "pidog"

    # ── Upstream PiDog (controller-control server on the Pi) ───────────
    pidog_ip: str = ""
    pidog_port: int = 8000

    # ── Persistent config file (local fallback, dual-write with object store)
    config_file: str = "/data/config.json"

    # ── Upstream proxy tuning ─────────────────────────────────────────
    pidog_proxy_max_concurrency: int = 2
    pidog_proxy_queue_size: int = 64
    pidog_proxy_request_timeout_s: float = 30.0
    pidog_camera_poll_interval_s: float = 0.2

    # ── NATS ──────────────────────────────────────────────────────────
    nats_url: str = "nats://localhost:4222"
    nats_ws_url: str = "ws://localhost:9222"
    nats_subject_prefix: str = "rt.v1"

    # ── Registry ──────────────────────────────────────────────────────
    registry_robots_bucket: str = "registry_robots"
    registry_heartbeat_s: float = 15.0
    connector_base_url: str = "http://localhost:8000"

    # ── Routing ───────────────────────────────────────────────────────
    base_path: str = ""

    # ── Persistent-config load ────────────────────────────────────────
    def model_post_init(self, __context: object) -> None:  # noqa: D401
        """Promote persisted config over env defaults (object store > local JSON)."""
        remote = config_store.load()
        if remote is not None:
            self._apply(remote)
            logger.info("Pidog config loaded from object store.")
            return

        local = self._read_local_json()
        if local is not None:
            self._apply(local)
            logger.info("Pidog config loaded from local JSON.")
            # Auto-migrate local → object store on first boot
            config_store.save(local)
            logger.info("Migrated local pidog config to object store.")
            return

        logger.info("No persisted pidog config; using env / defaults.")

    def _apply(self, data: dict) -> None:
        if "pidog_ip" in data:
            self.pidog_ip = str(data["pidog_ip"]).strip()
        if "pidog_port" in data:
            self.pidog_port = int(data["pidog_port"])

    def _read_local_json(self) -> dict | None:
        if not self.config_file:
            return None
        path = Path(self.config_file)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    # ── Persistence ───────────────────────────────────────────────────
    def save_pidog_endpoint(self, ip: str, port: int | None = None) -> None:
        """Persist a new upstream PiDog endpoint to both stores."""
        self.pidog_ip = ip.strip()
        if port is not None:
            self.pidog_port = int(port)
        data = {"pidog_ip": self.pidog_ip, "pidog_port": self.pidog_port}
        config_store.save(data)
        self._write_local_json(data)

    def refresh_persisted(self) -> bool:
        """Re-pull config from object store; True if anything changed."""
        remote = config_store.load()
        if remote is None:
            return False
        self._apply(remote)
        logger.info("Pidog config refreshed from object store.")
        return True

    def _write_local_json(self, data: dict) -> None:
        if not self.config_file:
            return
        try:
            path = Path(self.config_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.warning("Failed to write local pidog config JSON.", exc_info=True)

    # ── Computed helpers ──────────────────────────────────────────────
    @property
    def pidog_base_url(self) -> str:
        if not self.pidog_ip:
            return ""
        return f"http://{self.pidog_ip}:{self.pidog_port}"

    @property
    def is_configured(self) -> bool:
        return bool(self.pidog_ip)

    @property
    def base_path_normalized(self) -> str:
        v = self.base_path.strip()
        if v in ("", "/"):
            return ""
        if not v.startswith("/"):
            v = f"/{v}"
        return v.rstrip("/")

    @property
    def project_root(self) -> Path:
        # src/pidog_nova/settings.py  →  src/pidog_nova  →  src  →  <project>
        return Path(__file__).resolve().parent.parent.parent

    @property
    def static_directory(self) -> Path:
        return self.project_root / "static"

    @property
    def ui_directory(self) -> Path:
        # Self-contained HTML operator page (no build step).  Served at /ui
        # by main.py via StaticFiles(html=True) so visiting /ui/ renders
        # index.html directly.
        return self.project_root / "ui"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (singleton)."""
    return Settings()


def to_connector_settings(s: Settings | None = None):
    """Build a ``mobile_integration_sdk.ConnectorSettings`` from this config.

    The pidog env vars (ROBOT_MODEL, ROBOT_ID, NATS_URL, …) stay as the single
    source of truth; the SDK runtime then drives every spec-defined route,
    NATS subject, and registry bucket.
    """
    from mobile_integration_sdk import ConnectorSettings

    s = s or get_settings()
    return ConnectorSettings(
        robot_model=s.robot_model,
        robot_id=s.robot_id,
        source="pidog-nova",
        base_path=s.base_path_normalized,
        http_host=s.host,
        http_port=s.port,
        nats_url=s.nats_url,
        nats_subject_prefix=s.nats_subject_prefix,
        robots_bucket=s.registry_robots_bucket,
        registry_heartbeat_s=s.registry_heartbeat_s,
        connector_base_url=s.connector_base_url,
    )


# Convenience alias so ``from pidog_nova.settings import settings`` works.
settings: Settings = get_settings()
