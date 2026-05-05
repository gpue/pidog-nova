"""NATS KV registry for pidog-nova gateway.

Robot entries are **persistent** (no TTL) and carry an ``online`` flag so
consumers can distinguish live vs offline robots.  Entries are marked
offline on graceful shutdown and must be explicitly deleted via the
REST API to be fully removed.
"""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
import socket
from typing import Any

from nats.aio.client import Client as NATS

REGISTRY_BUCKET = os.getenv("REGISTRY_ROBOTS_BUCKET", "registry_robots")
CAMERA_BUCKET = os.getenv("REGISTRY_CAMERAS_BUCKET", "registry_cameras")
MODEL_BUCKET = os.getenv("REGISTRY_ROBOT_MODELS_BUCKET", "registry_robot_models")
CONFIG_BUCKET = os.getenv("REGISTRY_ROBOT_CONFIG_BUCKET", "robot_config")
HEARTBEAT_INTERVAL_S = float(os.getenv("REGISTRY_HEARTBEAT_S", "15"))
REGISTRY_KV_TTL_S = int(os.getenv("REGISTRY_KV_TTL_S", "0"))
CAMERA_KV_TTL_S = int(os.getenv("CAMERA_KV_TTL_S", "60"))
MODEL_KV_TTL_S = int(os.getenv("REGISTRY_MODEL_KV_TTL_S", "300"))
CONFIG_KV_TTL_S = int(os.getenv("REGISTRY_CONFIG_KV_TTL_S", "0"))  # permanent
NATS_URL = os.getenv("NATS_BROKER") or os.getenv("NATS_URL") or "nats://localhost:4222"
NATS_SUBJECT_PREFIX = os.getenv("NATS_SUBJECT_PREFIX", "rt.v1").strip(".")
CONNECTOR_BASE_URL = os.getenv(
    "CONNECTOR_BASE_URL", f"http://{socket.gethostname()}:8000"
).rstrip("/")
REGISTRY_BASE_URL = os.getenv("REGISTRY_BASE_URL", "").rstrip("/")
BASE_PATH = os.getenv("BASE_PATH", "").rstrip("/")
API_PREFIX = f"{BASE_PATH}/api" if BASE_PATH else "/api"


def _normalize_base_path(raw: str) -> str:
    value = (raw or "").strip()
    if value in {"", "/"}:
        return ""
    if not value.startswith("/"):
        value = f"/{value}"
    return value.rstrip("/")


def _api_prefix_from_base_path(raw: str) -> str:
    base_path = _normalize_base_path(raw)
    return f"{base_path}/api" if base_path else "/api"


def _browser_api_prefix_from_base_path(raw: str) -> str | None:
    base_path = _normalize_base_path(raw)
    if not base_path:
        return None
    return f"{base_path}/api"


def _join_base_url(base_url: str, base_path: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    path = _normalize_base_path(base_path)
    if not path:
        return base
    if base.endswith(path):
        return base
    return f"{base}{path}"


def _registry_base_url() -> str:
    base_url = REGISTRY_BASE_URL or CONNECTOR_BASE_URL
    return _join_base_url(base_url, BASE_PATH)


import logging

logger = logging.getLogger(__name__)


async def _ensure_kv_bucket(js: Any, bucket: str, description: str, ttl_s: int) -> Any:
    """Create the KV bucket or reconcile TTL on an existing one."""
    try:
        kv = await js.key_value(bucket)
    except Exception:
        from nats.js.api import KeyValueConfig

        await js.create_key_value(
            config=KeyValueConfig(
                bucket=bucket,
                history=1,
                ttl=ttl_s,
                description=description,
            )
        )
        return await js.key_value(bucket)

    status = await kv.status()
    current_ttl = status.ttl
    desired_ttl = float(ttl_s)
    if current_ttl != desired_ttl:
        dup_window = status.stream_info.config.duplicate_window
        if desired_ttl > 0 and dup_window > desired_ttl:
            dup_window = desired_ttl
        config = replace(
            status.stream_info.config,
            description=description,
            max_age=desired_ttl,
            duplicate_window=dup_window if desired_ttl > 0 else dup_window,
        )
        await js.update_stream(config=config)
        logger.info(
            "Updated KV bucket %s TTL from %s to %ss",
            bucket,
            current_ttl,
            ttl_s,
        )
        kv = await js.key_value(bucket)
    return kv


def _build_robot_payload(
    robot_model: str, robot_id: str, *, online: bool = True
) -> dict[str, Any]:
    subj_base = f"{NATS_SUBJECT_PREFIX}.robot.{robot_model}.{robot_id}"
    registry_base_url = _registry_base_url()
    payload = {
        "id": robot_id,
        "online": online,
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "endpoints": [
            f"{registry_base_url}/api/{robot_model}/{robot_id}",
        ],
        "metadata": {
            "kind": "physical",
            "robot_type": robot_model,
            "source": "pidog-nova",
            "connection_label": f"{robot_model}-{robot_id}",
            "command_subject": f"{subj_base}.control.cmd",
            "event_subject": f"{subj_base}.control.evt",
            "telemetry_subject": f"{subj_base}.telemetry.state",
            "capabilities": [
                "walk_control",
                "cameras",
                "graphnav",
                "graph_activate",
                "graph_navigate",
            ],
        },
    }
    browser_api_prefix = _browser_api_prefix_from_base_path(BASE_PATH)
    if browser_api_prefix:
        payload["browser_endpoint"] = f"{browser_api_prefix}/{robot_model}/{robot_id}"
    return payload


def _build_camera_payload(
    robot_id: str, robot_type: str, feed_name: str, api_prefix: str
) -> dict[str, Any]:
    registry_base_url = _registry_base_url()
    payload = {
        "robot_id": robot_id,
        "robot_type": robot_type,
        "feed": feed_name,
        "stream_url": f"{registry_base_url}/api/camera/{robot_id}/{feed_name}/video_feed",
        "source": "pidog-nova",
    }
    browser_api_prefix = _browser_api_prefix_from_base_path(BASE_PATH)
    if browser_api_prefix:
        payload["browser_stream_url"] = (
            f"{browser_api_prefix}/camera/{robot_id}/{feed_name}/video_feed"
        )
    return payload


class RegistryPublisher:
    def __init__(
        self,
        robot_model: str,
        robot_id: str,
        health_check: Callable[[], bool] | None = None,
    ) -> None:
        self._robot_model = robot_model
        self._robot_id = robot_id
        self._health_check = health_check
        self._nc: NATS | None = None
        self._robot_kv: Any = None
        self._camera_kv: Any = None
        self._model_kv: Any = None
        self._config_kv: Any = None
        self._heartbeat_task: asyncio.Task | None = None
        self._registered = False

    async def _connect(self) -> bool:
        if self._nc and self._nc.is_connected:
            return True
        try:
            nc = NATS()
            await nc.connect(servers=[NATS_URL])
            self._nc = nc
            js = nc.jetstream()
            self._robot_kv = await _ensure_kv_bucket(
                js,
                REGISTRY_BUCKET,
                "Robot registry entries (persistent, no TTL)",
                REGISTRY_KV_TTL_S,
            )
            self._camera_kv = await _ensure_kv_bucket(
                js, CAMERA_BUCKET, "Camera feed registry entries", CAMERA_KV_TTL_S
            )
            self._model_kv = await _ensure_kv_bucket(
                js, MODEL_BUCKET, "Robot model registry entries", MODEL_KV_TTL_S
            )
            self._config_kv = await _ensure_kv_bucket(
                js, CONFIG_BUCKET, "Robot config entries (permanent)", CONFIG_KV_TTL_S
            )
            return True
        except Exception:
            self._nc = None
            self._robot_kv = None
            self._camera_kv = None
            self._model_kv = None
            return False

    async def publish(self, *, online: bool = True) -> bool:
        if not await self._connect():
            self._registered = False
            return False
        payload = _build_robot_payload(self._robot_model, self._robot_id, online=online)
        try:
            await self._robot_kv.put(self._robot_id, json.dumps(payload).encode())
            self._registered = True
            return True
        except Exception:
            self._registered = False
            return False

    async def mark_offline(self) -> bool:
        """Mark this robot as offline in the KV bucket (entry persists)."""
        return await self.publish(online=False)

    async def unpublish(self) -> bool:
        """Permanently remove this robot from the KV bucket (decommission)."""
        if not await self._connect():
            return False
        try:
            await self._robot_kv.delete(self._robot_id)
            self._registered = False
            return True
        except Exception:
            return False

    async def publish_model(self) -> bool:
        """DEPRECATED: registry_robot_models is superseded by robot_config.

        This method is now a no-op. Capabilities are published via
        publish_config() to the robot_config KV bucket instead.
        """
        return True

    async def publish_config(self) -> bool:
        """Publish static robot config to the robot_config KV bucket (permanent)."""
        if not await self._connect():
            return False
        if self._config_kv is None:
            return False
        subj_base = f"{NATS_SUBJECT_PREFIX}.robot.{self._robot_model}.{self._robot_id}"
        registry_base_url = _registry_base_url()
        browser_api_prefix = _browser_api_prefix_from_base_path(BASE_PATH)
        payload = {
            "robot_id": self._robot_id,
            "robot_model": self._robot_model,
            "source": "pidog-nova",
            "kind": "physical",
            "endpoints": [
                f"{registry_base_url}/api/{self._robot_model}/{self._robot_id}",
            ],
            "browser_endpoint": (
                f"{browser_api_prefix}/{self._robot_model}/{self._robot_id}"
                if browser_api_prefix
                else None
            ),
            "nats_subjects": {
                "command": f"{subj_base}.control.cmd",
                "event": f"{subj_base}.control.evt",
                "telemetry": f"{subj_base}.telemetry.state",
            },
            "capabilities": [
                "walk_control",
                "cameras",
                "graphnav",
                "graph_activate",
                "graph_navigate",
            ],
            "available_actions": [],
            "available_modes": [],
        }
        try:
            data = json.dumps(payload).encode()
            await self._config_kv.put(self._robot_id, data)
            logger.info("Published robot config for '%s'", self._robot_id)
            return True
        except Exception as exc:
            logger.warning("Failed to publish robot config for '%s': %s", self._robot_id, exc)
            return False

    async def publish_cameras(self) -> bool:
        if not await self._connect():
            return False
        api_prefix = _api_prefix_from_base_path(BASE_PATH)
        key = f"{self._robot_id}.main"
        payload = _build_camera_payload(
            self._robot_id, self._robot_model, "main", api_prefix
        )
        try:
            await self._camera_kv.put(key, json.dumps(payload).encode())
            return True
        except Exception:
            return False

    async def unpublish_cameras(self) -> bool:
        if not await self._connect():
            return False
        try:
            await self._camera_kv.delete(f"{self._robot_id}.main")
        except Exception:
            pass
        return True

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            try:
                # Always publish the model — independent of robot health
                await self.publish_model()

                if self._health_check is not None:
                    healthy = await asyncio.to_thread(self._health_check)
                else:
                    healthy = True
                if healthy:
                    await self.publish(online=True)
                    await self.publish_cameras()
                elif self._registered:
                    await self.mark_offline()
                    await self.unpublish_cameras()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def start(self) -> None:
        # Always publish the model and config — even when the physical robot is unreachable
        await self.publish_model()
        await self.publish_config()

        if self._health_check is not None:
            healthy = await asyncio.to_thread(self._health_check)
        else:
            healthy = True
        if healthy:
            await self.publish(online=True)
            await self.publish_cameras()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """Stop heartbeat, mark robot offline, and disconnect."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        await self.unpublish_cameras()
        await self.mark_offline()
        if self._nc and self._nc.is_connected:
            try:
                await self._nc.drain()
            except Exception:
                pass
        self._nc = None
        self._robot_kv = None
        self._camera_kv = None
        self._model_kv = None
