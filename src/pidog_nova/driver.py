"""PidogDriver — implements ``mobile_integration_sdk`` capability Protocols
by delegating to the upstream PiDog ``controller-control`` server over HTTP.

Capability surface (advertised explicitly, **not** inferred):

* ``cameras``  — single ``main`` MJPEG feed via :class:`CameraStreamHub`
* ``recover``  — best-effort ``stand`` action (PiDog cannot self-right)

PiDog has no continuous-velocity control, no modes/lease/faults/power
endpoints, and no real GraphNav.  Those capabilities are intentionally
**not** advertised — the SDK will return ``501 Not Implemented`` for any
client that calls them.

Actions are exposed unconditionally (the SDK's actions router auto-mounts
when ``execute_action`` is defined); the static list mirrors the actions
supported by the upstream PiDog server.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from mobile_integration_sdk.driver.exceptions import (
    CapabilityNotSupported,
    DriverError,
    ResourceNotFound,
)
from mobile_integration_sdk.models import (
    ActionExecuteResponse,
    ActionListResponse,
    CameraEntry,
    CameraRotationResponse,
    ConfigResponse,
    HealthResponse,
    RecoverResponse,
    UpdateConfigRequest,
)
from mobile_integration_sdk.models.capabilities import Capability, CapabilitySet

from pidog_nova.camera_hub import CameraStreamHub
from pidog_nova.pidog_client import (
    PidogClient,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from pidog_nova.settings import settings

# Upstream PiDog action surface — hardcoded because the upstream
# controller-control server does not expose an action-list endpoint.
# Keep this in sync with the upstream Python action handlers.
_PIDOG_ACTIONS: tuple[str, ...] = (
    "stand",
    "sit",
    "lie",
    "head_bark",
    "wag_tail",
    "shake_head",
    "stretch",
    "push_up",
    "doze_off",
    "nod_lethargy",
    "forward",
    "backward",
    "turn_left",
    "turn_right",
    "trot",
    "half_sit",
    "head_up_down",
    "tilting_head",
)

# The PiDog upstream serves exactly one camera feed (Raspberry Pi camera v2).
_CAMERA_FEED = "main"

# Upstream state strings that imply the dog is actively driving.  Used to
# derive the canonical ``driving`` boolean from the raw ``state`` field.
_DRIVING_STATES: frozenset[str] = frozenset(
    {"walking", "moving", "forward", "backward", "trot"}
)


class PidogDriver:
    """SDK driver for SunFounder PiDog robots (HTTP-gateway connector)."""

    # Explicit capability override — drop WALK_CONTROL since PiDog has no
    # continuous-velocity control surface.  Actions, config and health are
    # always-on in the SDK; we only need to advertise the gated ones.
    capabilities = CapabilitySet({Capability.CAMERAS, Capability.RECOVER})

    def __init__(self) -> None:
        self._client = PidogClient(
            base_url_fn=lambda: settings.pidog_base_url,
            max_concurrency=settings.pidog_proxy_max_concurrency,
            max_queue_size=settings.pidog_proxy_queue_size,
            request_timeout=settings.pidog_proxy_request_timeout_s,
        )
        self._camera_hub = CameraStreamHub(
            poll_interval_s=settings.pidog_camera_poll_interval_s,
        )
        # Camera rotations are display-only; persisted in-memory per process
        # (consistent with unitree-nova). Persisting is a Phase 4+ concern.
        self._rotations: dict[str, int] = {}
        # Cached upstream-liveness flag, updated each telemetry tick by
        # ``snapshot_state``.  ``main.py`` reads it (synchronously) as the
        # RegistryPublisher's ``health_check`` so the KV ``online`` flag
        # reflects the upstream PiDog daemon's reachability, not just
        # whether the connector itself is up.
        self._upstream_alive: bool = False

    # ── Lifecycle hooks (called from main.py on_startup/on_shutdown) ──

    async def start(self) -> None:
        await self._client.start()
        await self._camera_hub.start()

    async def shutdown(self) -> None:
        await self._camera_hub.stop()
        await self._client.stop()

    # ── Required surface ─────────────────────────────────────────────

    def health(self) -> HealthResponse:
        # health() is synchronous in the SDK Protocol; we mirror the upstream
        # liveness flag via the most-recent state from PidogClient.  For Phase
        # 1 (no telemetry loop), we report "ok" iff the connector is at least
        # configured — a real probe happens async via /api/.../health.
        connected = settings.is_configured
        return HealthResponse(
            status="ok" if connected else "degraded",
            # The SDK spec enum allows {"mock", "real"} only. pidog-nova is a
            # gateway to real PiDog hardware, so "real" is the honest answer;
            # the surrounding `mode="gateway"` semantics live in the registry
            # entry's metadata instead.
            mode="real",
            robot_connected=connected,
            timestamp=datetime.now(UTC).isoformat(),
        )

    async def stop(self) -> dict[str, Any]:
        try:
            return await self._client.emergency_stop()
        except UpstreamUnavailableError as exc:
            raise DriverError(str(exc)) from exc
        except UpstreamTimeoutError as exc:
            raise DriverError(str(exc)) from exc

    # ── Telemetry sampler ────────────────────────────────────────────

    @property
    def upstream_alive(self) -> bool:
        """Last-known reachability of the upstream PiDog daemon.

        Updated each :meth:`snapshot_state` tick.  Used as the
        ``RegistryPublisher.health_check`` so the KV ``online`` flag
        tracks actual upstream reachability (the connector itself is
        always up while this process is running).
        """
        return self._upstream_alive

    async def snapshot_state(self) -> dict[str, Any] | None:
        """Poll the upstream ``/debug/state`` and ``/battery`` endpoints
        and return a canonical state snapshot.

        Returns ``None`` when the upstream is unconfigured (no IP set) so
        the SDK ``TelemetryPublisher`` skips publishing on that tick.
        Returns a dict when at least one of the two upstream polls
        succeeded; partial dicts with missing battery / state are valid
        and intentional — the consumer can detect them by the absence
        of the corresponding fields.
        """
        if not settings.is_configured:
            self._upstream_alive = False
            return None

        # Fire both polls concurrently; each handles its own errors and
        # returns ``{}`` on failure (so neither raises).
        battery_raw, state_raw = await asyncio.gather(
            self._client.fetch_battery(settings.robot_model, settings.robot_id),
            self._client.fetch_state(settings.robot_model, settings.robot_id),
        )

        # Liveness = "either poll returned something" — treat as alive
        # iff we actually got a non-empty response from the daemon.
        self._upstream_alive = bool(battery_raw) or bool(state_raw)
        if not self._upstream_alive:
            return None

        # ``robot_model`` / ``robot_id`` / ``source`` are filled by the SDK
        # ``TelemetryPublisher`` envelope itself (since SDK v0.3.2 the publisher
        # accepts ``source=`` at construction).  Returning only the actual
        # robot data here keeps the envelope canonical and avoids duplication.
        payload: dict[str, Any] = {}

        # Battery (canonical: 0..1 float, plus raw voltage + charging bool)
        if battery_raw:
            pct = battery_raw.get("percentage")
            if isinstance(pct, (int, float)):
                payload["battery"] = max(0.0, min(1.0, float(pct) / 100.0))
            volts = battery_raw.get("voltage")
            if isinstance(volts, (int, float)):
                payload["battery_voltage"] = float(volts)
            payload["charging"] = bool(battery_raw.get("charging", False))

        # Upstream state string → canonical 'state' + derived 'driving'
        if state_raw:
            state_str = str(state_raw.get("state", "")).lower()
            if state_str:
                payload["state"] = state_str
                payload["driving"] = state_str in _DRIVING_STATES

        return payload

    # ── Actions ──────────────────────────────────────────────────────

    async def list_actions(self) -> ActionListResponse:
        return ActionListResponse(actions=list(_PIDOG_ACTIONS), count=len(_PIDOG_ACTIONS))

    async def execute_action(self, name: str) -> ActionExecuteResponse:
        if name not in _PIDOG_ACTIONS:
            # SDK action router maps KeyError → 404; raise here for clarity.
            raise KeyError(f"unknown pidog action '{name}'")
        try:
            result = await self._client.execute_action(
                settings.robot_model, settings.robot_id, name
            )
        except UpstreamUnavailableError as exc:
            raise DriverError(str(exc)) from exc
        except UpstreamTimeoutError as exc:
            raise DriverError(str(exc)) from exc

        return ActionExecuteResponse(
            status=result.get("status", "ok"),
            action=name,
            action_id=result.get("action_id"),
            code=result.get("code", 0),
            message=result.get("message", ""),
        )

    # ── Recover (best-effort: PiDog cannot self-right) ────────────────

    async def recover(self) -> RecoverResponse:
        """PiDog cannot self-right from a physical fall.

        We attempt the ``stand`` upstream action; if it succeeds the dog is
        re-posed but may still need physical help.  Status is therefore
        ``partial`` rather than ``ok`` (mirrors legacy behaviour).
        """
        try:
            result = await self._client.execute_action(
                settings.robot_model, settings.robot_id, "stand"
            )
        except (UpstreamUnavailableError, UpstreamTimeoutError) as exc:
            return RecoverResponse(
                status="error",
                steps_completed=[],
                steps_failed=["stand"],
                message=str(exc),
                remaining_faults=0,
            )

        if result.get("status") == "ok":
            return RecoverResponse(
                status="partial",
                steps_completed=["stand"],
                steps_failed=["self_right"],
                message=(
                    "PiDog cannot self-right physically; legs re-posed via 'stand' action"
                ),
                remaining_faults=0,
            )

        return RecoverResponse(
            status="error",
            steps_completed=[],
            steps_failed=["stand"],
            message=result.get("message", "upstream stand action failed"),
            remaining_faults=0,
        )

    # ── Config ───────────────────────────────────────────────────────

    async def get_config(self) -> ConfigResponse:
        return ConfigResponse(
            # Spec enum {"mock", "real"} only — see HealthResponse comment.
            mode="real",
            robot_model=settings.robot_model,
            robot_ip=settings.pidog_ip or "",
            network_interface="",  # not used by pidog
            enable_lease=False,
            log_level=settings.log_level.upper(),
        )

    async def update_config(self, request: UpdateConfigRequest) -> ConfigResponse:
        ip = (request.robot_ip or "").strip()
        if not ip:
            # Surface as 400 via the SDK's DriverError handler (defaults to 500;
            # this is a Phase 4+ spec drift to track).
            raise DriverError("robot_ip required")
        settings.save_pidog_endpoint(ip)
        return await self.get_config()

    # ── Cameras ──────────────────────────────────────────────────────

    def list_cameras(self) -> list[CameraEntry]:
        base = settings.connector_base_url.rstrip("/")
        return [
            CameraEntry(
                robot_id=settings.robot_id,
                robot_type=settings.robot_model,
                feed=_CAMERA_FEED,
                stream_url=(
                    f"{base}/api/camera/{settings.robot_id}/{_CAMERA_FEED}/video_feed"
                ),
                snapshot_url=(
                    f"{base}/api/camera/{settings.robot_id}/{_CAMERA_FEED}/snapshot"
                ),
                source="pidog-nova",
                rotation=self._rotations.get(_CAMERA_FEED, 0),
            )
        ]

    async def camera_sources(self, robot_id: str) -> dict[str, Any]:
        return {
            "robot_id": robot_id,
            "sources": [
                {
                    "name": entry.feed,
                    "label": entry.feed.title(),
                    "rotation": entry.rotation,
                }
                for entry in self.list_cameras()
            ],
        }

    async def get_camera_rotation(
        self, robot_id: str, feed: str
    ) -> CameraRotationResponse:
        return CameraRotationResponse(
            robot_id=robot_id,
            feed_name=feed,
            rotation=self._rotations.get(feed, 0),
        )

    async def set_camera_rotation(
        self, robot_id: str, feed: str, rotation: int
    ) -> CameraRotationResponse:
        if feed != _CAMERA_FEED:
            raise ResourceNotFound(f"camera feed '{feed}' not available")
        self._rotations[feed] = int(rotation)
        return CameraRotationResponse(
            robot_id=robot_id, feed_name=feed, rotation=self._rotations[feed]
        )

    async def camera_video_feed(
        self,
        robot_id: str,
        feed: str,
        quality: int | None = None,
        fps: float | None = None,
        *,
        request: Request | None = None,
    ):
        if feed != _CAMERA_FEED:
            raise ResourceNotFound(f"camera feed '{feed}' not available")
        if not settings.is_configured:
            raise CapabilityNotSupported("Pidog IP not configured")
        snapshot_url = self._client.snapshot_url(robot_id, feed)
        # The SDK v0.3 camera router introspects this method's signature
        # and threads the live FastAPI Request through as ``request=``,
        # so the camera hub can call ``request.is_disconnected()`` per
        # frame and tear the stream down promptly when the client goes
        # away. Direct callers (tests, scripts) omit ``request`` and we
        # fall back to a no-op stand-in that never reports a disconnect
        # — the per-frame 30 s timeout caps the stream lifetime in that
        # case.
        req: Any = request if request is not None else _NullRequest()
        return await self._camera_hub.stream_response(req, snapshot_url)

    async def camera_snapshot(
        self, robot_id: str, feed: str, quality: int | None = None
    ) -> bytes:
        if feed != _CAMERA_FEED:
            raise ResourceNotFound(f"camera feed '{feed}' not available")
        if not settings.is_configured:
            raise CapabilityNotSupported("Pidog IP not configured")
        snapshot_url = self._client.snapshot_url(robot_id, feed)
        frame = await self._camera_hub.get_snapshot(snapshot_url)
        if frame is None:
            raise DriverError("upstream snapshot unavailable")
        return frame

    # ── Helpers ──────────────────────────────────────────────────────


class _NullRequest:
    """No-op stand-in for FastAPI Request, used only when
    :meth:`PidogDriver.camera_video_feed` is invoked outside an HTTP
    context (tests, scripts).  Reports the connection as live forever;
    the camera hub's per-frame 30 s timeout still caps stream lifetime.
    """

    async def is_disconnected(self) -> bool:
        return False


__all__ = ["PidogDriver"]
