"""Nova Object Store client for pidog-nova configuration persistence."""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_NOVA_API_GATEWAY = os.getenv(
    "NOVA_API_GATEWAY",
    "http://api-gateway.wandelbots.svc.cluster.local:8080",
)
_NOVA_CELL_ID = os.getenv("NOVA_CELL_ID", "cell")
_STORE_KEY = "config/pidog-nova"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _url() -> str:
    base = _NOVA_API_GATEWAY.rstrip("/")
    return f"{base}/api/v1/cells/{_NOVA_CELL_ID}/store/objects/{_STORE_KEY}"


def load() -> dict | None:
    """GET config JSON from the Nova Object Store. Returns None on any failure."""
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(_url())
            if r.status_code == 404:
                logger.info("No config found in object store (404).")
                return None
            r.raise_for_status()
            return r.json()
    except Exception:
        logger.warning("Failed to load config from object store.", exc_info=True)
        return None


def save(data: dict) -> bool:
    """PUT config JSON to the Nova Object Store (multipart/form-data)."""
    try:
        json_bytes = json.dumps(data, indent=2).encode("utf-8")
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.put(
                _url(),
                files={"file": ("config.json", json_bytes, "application/json")},
            )
            r.raise_for_status()
        logger.info("Config saved to object store.")
        return True
    except Exception:
        logger.warning("Failed to save config to object store.", exc_info=True)
        return False


def exists() -> bool:
    """HEAD check — does config exist in the object store?"""
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.head(_url())
            return r.status_code == 200
    except Exception:
        return False
