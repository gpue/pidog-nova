"""Nova Object Store client for pidog-nova configuration persistence.

Pidog connector configuration (currently just ``pidog_ip``/``pidog_port`` of
the upstream ``controller-control`` server) is dual-written to:

1. The Nova Object Store (canonical, shared across pod restarts), and
2. A local JSON file (fallback / dev mode).

The local JSON is also the migration source on first boot — if the object
store is empty but a local config exists, we auto-promote it.
"""

from __future__ import annotations

import json
import os

import httpx

from pidog_nova._utils import logger

_NOVA_API_GATEWAY = os.getenv(
    "NOVA_API_GATEWAY",
    "http://api-gateway.wandelbots.svc.cluster.local:8080",
)
_NOVA_CELL_ID = os.getenv("NOVA_CELL_ID", "cell")
_ROBOT_ID = os.getenv("ROBOT_ID", "pidog")
_STORE_KEY = f"config/pidog-nova/{_ROBOT_ID}"
# Short connect timeout so dev environments (where the object store DNS does
# not resolve) fail fast and fall through to local-JSON / env defaults.
_TIMEOUT = httpx.Timeout(60.0, connect=2.0)


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
