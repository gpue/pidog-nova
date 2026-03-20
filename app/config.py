"""Pidog Nova Gateway configuration."""

import json
import os
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.robot_model = os.getenv("ROBOT_MODEL", "pidog")
        self.robot_id = os.getenv("ROBOT_ID", "pidog")
        self.config_file = os.getenv("CONFIG_FILE", "/data/config.json")
        self.pidog_ip = os.getenv("PIDOG_IP", "")
        self.pidog_port = int(os.getenv("PIDOG_PORT", "8000"))
        self.proxy_max_concurrency = int(os.getenv("PIDOG_PROXY_MAX_CONCURRENCY", "2"))
        self.proxy_queue_size = int(os.getenv("PIDOG_PROXY_QUEUE_SIZE", "64"))
        self._load_config()

    def _load_config(self) -> None:
        if not self.config_file:
            return
        path = Path(self.config_file)
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text())
            if "pidog_ip" in data:
                self.pidog_ip = str(data["pidog_ip"]).strip()
            if "pidog_port" in data:
                self.pidog_port = int(data["pidog_port"])
        except Exception:
            pass

    def save_config(self, pidog_ip: str, pidog_port: int | None = None) -> None:
        if not self.config_file:
            return
        path = Path(self.config_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, str | int] = {"pidog_ip": pidog_ip.strip()}
        if pidog_port is not None:
            data["pidog_port"] = pidog_port
        path.write_text(json.dumps(data, indent=2))
        self.pidog_ip = pidog_ip.strip()
        if pidog_port is not None:
            self.pidog_port = pidog_port

    @property
    def pidog_base_url(self) -> str:
        if not self.pidog_ip:
            return ""
        return f"http://{self.pidog_ip}:{self.pidog_port}"

    @property
    def is_configured(self) -> bool:
        return bool(self.pidog_ip)


settings = Settings()
