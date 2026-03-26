"""Pidog Nova Gateway configuration.

Phase 1 — dual-write: reads from object store first, falls back to local JSON,
auto-migrates local JSON → object store on first boot if needed.
"""

import json
import logging
import os
from pathlib import Path

from app import config_store

logger = logging.getLogger(__name__)


class Settings:
    def __init__(self) -> None:
        self.robot_model = os.getenv("ROBOT_MODEL", "pidog")
        self.robot_id = os.getenv("ROBOT_ID", "pidog")
        self.config_file = os.getenv("CONFIG_FILE", "/data/config.json")
        self.pidog_ip = os.getenv("PIDOG_IP", "")
        self.pidog_port = int(os.getenv("PIDOG_PORT", "8000"))
        self.proxy_max_concurrency = int(os.getenv("PIDOG_PROXY_MAX_CONCURRENCY", "2"))
        self.proxy_queue_size = int(os.getenv("PIDOG_PROXY_QUEUE_SIZE", "64"))
        self.camera_poll_interval_s = float(
            os.getenv("PIDOG_CAMERA_POLL_INTERVAL_S", "0.2")
        )
        self._load_config()

    # --- persistence (object store > local JSON > env > default) --------

    def _load_config(self) -> None:
        """Load persisted config.  Precedence: object store > local JSON > env."""
        remote = config_store.load()
        if remote is not None:
            self._apply(remote)
            logger.info("Config loaded from object store.")
            return

        # Fallback: local JSON
        local = self._read_local_json()
        if local is not None:
            self._apply(local)
            logger.info("Config loaded from local JSON (object store empty).")
            # Auto-migrate to object store
            config_store.save(local)
            logger.info("Migrated local config to object store.")
            return

        logger.info("No persisted config found; using env / defaults.")

    def _apply(self, data: dict) -> None:
        """Apply a config dict onto this Settings instance."""
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

    def _persistable(self) -> dict:
        """Return the dict that should be persisted."""
        return {"pidog_ip": self.pidog_ip, "pidog_port": self.pidog_port}

    def save_config(self, pidog_ip: str, pidog_port: int | None = None) -> None:
        self.pidog_ip = pidog_ip.strip()
        if pidog_port is not None:
            self.pidog_port = pidog_port

        data = self._persistable()

        # Dual-write: object store + local JSON (Phase 1)
        config_store.save(data)
        self._write_local_json(data)

    def _write_local_json(self, data: dict) -> None:
        if not self.config_file:
            return
        try:
            path = Path(self.config_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.warning("Failed to write local config JSON.", exc_info=True)

    def refresh(self) -> bool:
        """Re-pull config from the object store.  Returns True if new data applied."""
        remote = config_store.load()
        if remote is None:
            return False
        self._apply(remote)
        logger.info("Config refreshed from object store.")
        return True

    @property
    def pidog_base_url(self) -> str:
        if not self.pidog_ip:
            return ""
        return f"http://{self.pidog_ip}:{self.pidog_port}"

    @property
    def is_configured(self) -> bool:
        return bool(self.pidog_ip)


settings = Settings()
