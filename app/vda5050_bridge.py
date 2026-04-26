"""VDA5050 bridge for pidog-nova.

Polls the upstream PiDog device via REST for state, publishes VDA5050
state/visualization/connection/factsheet on NATS, and subscribes to
VDA5050 order/instantActions to translate into REST calls to the PiDog.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from nats.aio.client import Client as NATS

from nova_vda5050 import (
    TopicMapper,
    get_manufacturer,
    build_factsheet,
    action_def,
)
from nova_vda5050.connection_helpers import robot_online, robot_offline
from nova_vda5050.schemas import (
    AgvPosition,
    BatteryState,
    SafetyState,
    Velocity,
)
from nova_vda5050.schemas.state import StateMessage
from nova_vda5050.schemas.visualization import VisualizationMessage
from nova_vda5050.schemas.instant_actions import InstantActionsMessage
from nova_vda5050.schemas.order import OrderMessage
from nova_vda5050.commands import translate_order_to_nova

logger = logging.getLogger(__name__)

# Mapping from VDA5050-style action names to PiDog REST action names
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


class PiDogVDA5050Bridge:
    """Full VDA5050 bridge for PiDog: telemetry polling + command translation."""

    def __init__(
        self,
        nc: NATS,
        robot_model: str,
        robot_id: str,
        pidog_base_url_fn: Any,
        nats_prefix: str = "rt.v1",
    ):
        self._nc = nc
        self._robot_model = robot_model
        self._robot_id = robot_id
        self._pidog_base_url_fn = pidog_base_url_fn  # callable returning str
        self._mapper = TopicMapper(nats_prefix)
        self._header_counters: dict[str, int] = {}
        self._poll_task: asyncio.Task | None = None
        self._running = False
        self._http = httpx.AsyncClient(timeout=3.0)

    def _next_header(self, key: str) -> int:
        self._header_counters[key] = self._header_counters.get(key, 0) + 1
        return self._header_counters[key]

    @property
    def _base_url(self) -> str:
        return self._pidog_base_url_fn()

    @property
    def _manufacturer(self) -> str:
        return get_manufacturer(self._robot_model)

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the bridge: publish online + factsheet, subscribe inbound, start polling."""
        # Connection ONLINE
        msg = robot_online(self._robot_model, self._robot_id)
        subject = self._mapper.nova_connection_nats(self._robot_model, self._robot_id)
        await self._publish(subject, msg.model_dump())
        logger.info("[vda5050-pidog] Published ONLINE")

        # Factsheet
        await self._publish_factsheet()

        # Subscribe to inbound VDA5050 messages
        wildcard = self._mapper.vda5050_inbound_wildcard()
        await self._nc.subscribe(wildcard, cb=self._handle_inbound)
        logger.info("[vda5050-pidog] Subscribed to inbound: %s", wildcard)

        # Start telemetry polling
        self._running = True
        self._poll_task = asyncio.create_task(self._telemetry_loop())
        logger.info(
            "[vda5050-pidog] Bridge started for %s/%s",
            self._robot_model,
            self._robot_id,
        )

    async def stop(self) -> None:
        """Stop polling and publish OFFLINE."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        msg = robot_offline(self._robot_model, self._robot_id)
        subject = self._mapper.nova_connection_nats(self._robot_model, self._robot_id)
        await self._publish(subject, msg.model_dump())
        logger.info("[vda5050-pidog] Published OFFLINE")

        await self._http.aclose()

    # ── Telemetry polling ────────────────────────────────────────────────

    async def _telemetry_loop(self) -> None:
        """Poll PiDog REST API for state and publish VDA5050 messages."""
        while self._running:
            try:
                await self._poll_and_publish()
            except Exception as exc:
                logger.debug("[vda5050-pidog] Poll error: %s", exc)
            await asyncio.sleep(2.0)

    async def _poll_and_publish(self) -> None:
        base = self._base_url
        if not base:
            return

        model = self._robot_model
        rid = self._robot_id
        manufacturer = self._manufacturer

        # Poll battery
        battery_charge = 0.0
        battery_voltage = 0.0
        charging = False
        try:
            r = await self._http.get(f"{base}/api/{model}/{rid}/battery")
            if r.status_code == 200:
                data = r.json()
                battery_charge = float(data.get("percentage", 0))
                battery_voltage = float(data.get("voltage", 0))
                charging = bool(data.get("charging", False))
        except Exception:
            pass

        # Poll debug state for robot status
        driving = False
        try:
            r = await self._http.get(f"{base}/api/{model}/{rid}/debug/state")
            if r.status_code == 200:
                data = r.json()
                state_str = str(data.get("state", "")).lower()
                driving = state_str in (
                    "walking",
                    "moving",
                    "forward",
                    "backward",
                    "trot",
                )
        except Exception:
            pass

        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        # Build state message
        state_msg = StateMessage(
            headerId=self._next_header("state"),
            timestamp=now,
            manufacturer=manufacturer,
            serialNumber=rid,
            orderId="",
            orderUpdateId=0,
            lastNodeId="",
            lastNodeSequenceId=0,
            driving=driving,
            paused=False,
            newBaseRequest=False,
            operatingMode="AUTOMATIC",
            agvPosition=AgvPosition(x=0, y=0, theta=0, mapId="default"),
            velocity=Velocity(),
            batteryState=BatteryState(
                batteryCharge=battery_charge,
                batteryVoltage=battery_voltage,
                charging=charging,
            ),
            safetyState=SafetyState(),
            nodeStates=[],
            edgeStates=[],
            actionStates=[],
            errors=[],
            informations=[],
        )
        state_subject = self._mapper.nova_state_to_vda5050_nats(model, rid)
        await self._publish(state_subject, state_msg.model_dump())

        # Build visualization message
        viz_msg = VisualizationMessage(
            headerId=self._next_header("viz"),
            timestamp=now,
            manufacturer=manufacturer,
            serialNumber=rid,
            agvPosition=AgvPosition(x=0, y=0, theta=0, mapId="default"),
            velocity=Velocity(),
        )
        viz_subject = self._mapper.nova_state_to_vda5050_visualization_nats(model, rid)
        await self._publish(viz_subject, viz_msg.model_dump())

    # ── Factsheet ────────────────────────────────────────────────────────

    async def _publish_factsheet(self) -> None:
        actions = [
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
        ]
        msg = build_factsheet(self._robot_model, self._robot_id, actions)
        subject = self._mapper.nova_factsheet_nats(self._robot_model, self._robot_id)
        await self._publish(subject, msg.model_dump(by_alias=True))
        logger.info("[vda5050-pidog] Published factsheet")

    # ── Inbound command handling ─────────────────────────────────────────

    async def _handle_inbound(self, msg: Any) -> None:
        addr = self._mapper.parse_vda5050_nats_subject(msg.subject)
        if addr is None:
            return
        if addr.serial_number != self._robot_id:
            return

        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except Exception:
            logger.warning("[vda5050-pidog] Invalid JSON on %s", msg.subject)
            return

        if addr.message_type == "instantActions":
            await self._handle_instant_actions(payload)
        elif addr.message_type == "order":
            await self._handle_order(payload)

    async def _handle_instant_actions(self, payload: dict) -> None:
        try:
            msg = InstantActionsMessage.model_validate(payload)
        except Exception as exc:
            logger.warning("[vda5050-pidog] Invalid instantActions: %s", exc)
            return

        base = self._base_url
        if not base:
            return
        model = self._robot_model
        rid = self._robot_id

        for action in msg.actions:
            action_type = action.actionType
            logger.info("[vda5050-pidog] Executing instant action: %s", action_type)

            try:
                if action_type in ("stop", "estop"):
                    await self._http.post(f"{base}/api/{model}/{rid}/stop")
                elif action_type == "enable":
                    await self._http.post(f"{base}/api/{model}/{rid}/action/stand")
                elif action_type == "disable":
                    await self._http.post(f"{base}/api/{model}/{rid}/action/sit")
                elif action_type in _VDA5050_TO_PIDOG_ACTION:
                    pidog_action = _VDA5050_TO_PIDOG_ACTION[action_type]
                    await self._http.post(
                        f"{base}/api/{model}/{rid}/action/{pidog_action}"
                    )
                else:
                    # Try as raw PiDog action name
                    await self._http.post(
                        f"{base}/api/{model}/{rid}/action/{action_type}"
                    )
            except Exception as exc:
                logger.warning("[vda5050-pidog] Action %s failed: %s", action_type, exc)

    async def _handle_order(self, payload: dict) -> None:
        try:
            msg = OrderMessage.model_validate(payload)
        except Exception as exc:
            logger.warning("[vda5050-pidog] Invalid order: %s", exc)
            return

        base = self._base_url
        if not base:
            return
        model = self._robot_model
        rid = self._robot_id

        # Translate order nodes to Nova commands, then execute sequentially
        commands = translate_order_to_nova(msg)
        for cmd in commands:
            cmd_type = cmd.get("type", "")
            logger.info("[vda5050-pidog] Executing order command: %s", cmd_type)
            try:
                if cmd_type == "stop":
                    await self._http.post(f"{base}/api/{model}/{rid}/stop")
                elif cmd_type == "navigate":
                    # PiDog doesn't have real navigation, use forward as placeholder
                    await self._http.post(f"{base}/api/{model}/{rid}/action/forward")
                elif cmd_type in _VDA5050_TO_PIDOG_ACTION:
                    pidog_action = _VDA5050_TO_PIDOG_ACTION[cmd_type]
                    await self._http.post(
                        f"{base}/api/{model}/{rid}/action/{pidog_action}"
                    )
            except Exception as exc:
                logger.warning("[vda5050-pidog] Command %s failed: %s", cmd_type, exc)

    # ── Internal ─────────────────────────────────────────────────────────

    async def _publish(self, subject: str, payload: dict) -> None:
        if self._nc is None or not self._nc.is_connected:
            return
        try:
            await self._nc.publish(subject, json.dumps(payload).encode("utf-8"))
        except Exception as exc:
            logger.debug("[vda5050-pidog] Publish failed on %s: %s", subject, exc)
