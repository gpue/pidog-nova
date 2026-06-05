"""Adapter that plugs the VDA5050 bridge into the SDK lifecycle.

The SDK owns NATS, telemetry, and control.  This adapter exposes the
``start()`` / ``stop()`` shape the SDK lifespan expects and internally:

* subscribes to VDA5050 ``order`` / ``instantActions``,
* publishes ONLINE + factsheet on start,
* drives the driver's :meth:`snapshot_state` on a timer and republishes
  the result as VDA5050 ``state`` and ``visualization`` messages,
* dispatches inbound action types via the PiDog-specific name table back
  into the driver's :meth:`execute_action` / :meth:`emergency_stop`,
* publishes OFFLINE on stop.

This keeps the SDK clean of any VDA5050 specifics while sharing the
SDK's NATS connection and the driver's existing upstream-aware sampler
(so VDA5050 sees the same data the SDK telemetry pump publishes — no
duplicate polling of the upstream PiDog daemon).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from nova_vda5050 import action_def
from nova_vda5050.schemas.factsheet import MobileRobotActionDef
from nova_vda5050.schemas.order import OrderMessage

from pidog_nova.driver import PidogDriver
from pidog_nova.settings import settings
from pidog_nova.vda5050_bridge import VDA5050Bridge

logger = logging.getLogger(__name__)


# Mapping from VDA5050-style action names (camelCase, per VDA5050
# convention) to the upstream PiDog REST action names (snake_case).
# Unknown action types fall through to the raw PiDog action name.
_VDA5050_TO_PIDOG_ACTION: dict[str, str] = {
    "stand": "stand",
    "sit": "sit",
    "lie": "lie",
    "bark": "head_bark",
    "wagTail": "wag_tail",
    "shakeHead": "shake_head",
    "stretch": "stretch",
    "pushUp": "push_up",
    "dozeOff": "doze_off",
    "nod": "nod_lethargy",
    "forward": "forward",
    "backward": "backward",
    "turnLeft": "turn_left",
    "turnRight": "turn_right",
    "trot": "trot",
    "halfSit": "half_sit",
    "headUpDown": "head_up_down",
    "tiltingHead": "tilting_head",
}


def _build_factsheet_actions() -> list[MobileRobotActionDef]:
    """Build the VDA5050 factsheet action list for PiDog."""
    return [
        # ── Lifecycle (standard Nova set) ────────────────────────────
        action_def("enable", "Stand up", ["INSTANT"], ["NONE"]),
        action_def("disable", "Sit down", ["INSTANT"], ["HARD"]),
        action_def(
            "stop",
            "Emergency stop",
            ["INSTANT"],
            ["HARD"],
            pause_allowed="false",
            cancel_allowed="false",
        ),
        action_def(
            "estop",
            "Emergency stop",
            ["INSTANT"],
            ["HARD"],
            pause_allowed="false",
            cancel_allowed="false",
        ),
        # ── PiDog-specific actions (camelCase per VDA5050 style) ─────
        action_def("stand", "Stand pose", ["INSTANT"], ["HARD"]),
        action_def("sit", "Sit pose", ["INSTANT"], ["HARD"]),
        action_def("lie", "Lie down", ["INSTANT"], ["HARD"]),
        action_def("bark", "Bark animation", ["INSTANT"], ["HARD"]),
        action_def("wagTail", "Wag tail", ["INSTANT"], ["SOFT"]),
        action_def("shakeHead", "Shake head", ["INSTANT"], ["SOFT"]),
        action_def("stretch", "Stretch", ["INSTANT"], ["HARD"]),
        action_def("pushUp", "Push-up", ["INSTANT"], ["HARD"]),
        action_def("dozeOff", "Doze off", ["INSTANT"], ["HARD"]),
        action_def("nod", "Nod", ["INSTANT"], ["SOFT"]),
        action_def("forward", "Walk forward", ["INSTANT", "NODE"], ["NONE"]),
        action_def("backward", "Walk backward", ["INSTANT"], ["NONE"]),
        action_def("turnLeft", "Turn left", ["INSTANT"], ["NONE"]),
        action_def("turnRight", "Turn right", ["INSTANT"], ["NONE"]),
        action_def("trot", "Trot gait", ["INSTANT"], ["NONE"]),
        action_def("halfSit", "Half-sit pose", ["INSTANT"], ["HARD"]),
        action_def("headUpDown", "Head up/down", ["INSTANT"], ["SOFT"]),
        action_def("tiltingHead", "Tilting head", ["INSTANT"], ["SOFT"]),
    ]


def _to_vda5050_shape(sample: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert PiDog's flat snapshot to the nested shape expected by
    :func:`nova_vda5050.transform.transform_telemetry_to_state`.

    PiDog's sampler returns ``{battery: 0..1 float, battery_voltage,
    charging, state, driving, ...}`` (flat) — the canonical shape used
    everywhere else in the SDK telemetry path.  VDA5050's transform
    expects ``battery`` to be a ``{percentage, voltage, charging}``
    sub-dict, so we adapt here rather than mutate the canonical
    snapshot.

    Returns ``None`` if the input is ``None`` (upstream unreachable),
    which the publish loop uses as the signal to skip publishing.
    """
    if sample is None:
        return None

    bat: dict[str, Any] = {}
    raw_bat = sample.get("battery")
    if isinstance(raw_bat, (int, float)):
        # PiDog stores 0..1; VDA5050 batteryCharge is a percentage 0..100.
        bat["percentage"] = float(raw_bat) * 100.0
    if "battery_voltage" in sample:
        bat["voltage"] = float(sample["battery_voltage"])
    if "charging" in sample:
        bat["charging"] = bool(sample["charging"])

    return {
        "battery": bat,
        "status": {"driving": bool(sample.get("driving", False))},
        # No position/velocity: PiDog has no odometry.
    }


class Vda5050Adapter:
    """Lifespan-compatible wrapper around :class:`VDA5050Bridge`.

    Parameters
    ----------
    nc:
        Live NATS client (``NatsConnection.primary``) — never the wrapper.
    driver:
        The PiDog driver; used both as the state-sample source and the
        target for translated VDA5050 commands.
    publish_interval_s:
        Frequency at which to emit VDA5050 ``state`` + ``visualization``.
        2 s matches the legacy bridge and is conservative — PiDog moves
        slowly enough that higher rates would just waste bandwidth.
    """

    def __init__(
        self,
        *,
        nc: Any,
        driver: PidogDriver,
        publish_interval_s: float = 2.0,
        nats_prefix: str | None = None,
    ) -> None:
        self._nc = nc
        self._driver = driver
        self._interval = publish_interval_s
        self._bridge = VDA5050Bridge(
            nats_client=nc,
            nats_prefix=nats_prefix or settings.nats_subject_prefix,
        )
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._model = settings.robot_model
        self._id = settings.robot_id

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._bridge.start_inbound_subscriptions(
            on_instant_action=self._handle_instant_action,
            on_order=self._handle_order,
        )
        await self._bridge.publish_online(self._model, self._id)
        await self._bridge.publish_factsheet(
            self._model, self._id, _build_factsheet_actions()
        )
        self._task = asyncio.create_task(self._publish_loop(), name="vda5050-publish")
        logger.info("[vda5050] adapter started for %s/%s", self._model, self._id)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        try:
            await self._bridge.publish_offline(self._model, self._id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[vda5050] publish_offline failed: %s", exc)
        logger.info("[vda5050] adapter stopped")

    # ── internals ────────────────────────────────────────────────────────

    async def _publish_loop(self) -> None:
        """Sample driver state and publish VDA5050 state + visualization.

        When the sampler returns ``None`` (upstream unconfigured /
        unreachable) we just skip publishing for this tick — the VDA5050
        consumer sees stale state and can detect the gap.  We don't
        republish OFFLINE on every miss because that'd be noisy; the
        OFFLINE message is reserved for graceful shutdown.
        """
        while not self._stopping.is_set():
            try:
                sample = await self._driver.snapshot_state()
                shaped = _to_vda5050_shape(sample)
                if shaped is not None:
                    await self._bridge.publish_state(self._model, self._id, shaped)
                    await self._bridge.publish_visualization(
                        self._model, self._id, shaped
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("[vda5050] publish loop error: %s", exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)

    async def _handle_instant_action(
        self, robot_id: str, action_type: str, params: dict[str, Any]
    ) -> None:
        """Dispatch an inbound VDA5050 instant action to the driver.

        Mapping rules (mirror the legacy bridge):

        * ``stop`` / ``estop``         → ``driver.stop()`` (the SDK's
          motion-stop convention — internally POSTs upstream ``/stop``)
        * ``enable``                   → ``driver.execute_action("stand")``
        * ``disable``                  → ``driver.execute_action("sit")``
        * ``<known camelCase action>`` → upstream snake_case via
          :data:`_VDA5050_TO_PIDOG_ACTION`
        * anything else                → passed through verbatim as a
          raw PiDog action name (the upstream daemon will reject unknown
          names with a 4xx, which we log at WARNING via the bridge).
        """
        if robot_id != self._id:
            return

        try:
            if action_type in ("stop", "estop"):
                await self._driver.stop()
                return
            if action_type == "enable":
                await self._driver.execute_action("stand")
                return
            if action_type == "disable":
                await self._driver.execute_action("sit")
                return

            pidog_name = _VDA5050_TO_PIDOG_ACTION.get(action_type, action_type)
            await self._driver.execute_action(pidog_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[vda5050] action dispatch failed (type=%s, params=%s): %s",
                action_type,
                params,
                exc,
            )

    async def _handle_order(self, robot_id: str, order: OrderMessage) -> None:
        """PiDog has no real navigation, so we currently just log inbound
        orders.  Honouring the order vocabulary would require either
        a (yet-to-exist) upstream waypoint API or a translation layer
        that decomposes nodes into a sequence of ``forward`` /
        ``turnLeft`` / ``turnRight`` actions; both are out of scope for
        Phase 3.
        """
        if robot_id != self._id:
            return
        logger.info(
            "[vda5050] received order %s with %d nodes (ignored — PiDog has "
            "no navigation)",
            order.orderId,
            len(order.nodes or []),
        )


__all__ = ["Vda5050Adapter"]
