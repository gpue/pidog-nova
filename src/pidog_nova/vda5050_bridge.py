"""VDA5050 bridge for pidog-nova.

Thin wrapper around ``nova_vda5050`` that exposes a SDK-friendly shape:
publish helpers for state / visualization / connection / factsheet, and
inbound subscription with a pluggable callback.

The class is intentionally generic — PiDog-specific concerns (the action
set published in the factsheet, the mapping from VDA5050 action names to
upstream PiDog action names) live in :mod:`pidog_nova.vda5050_adapter`,
which wires this bridge into the SDK lifespan.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from nats.aio.client import Client as NATS
from nova_vda5050 import (
    TopicMapper,
    build_factsheet,
    get_manufacturer,
    transform_telemetry_to_state,
)
from nova_vda5050.connection_helpers import robot_offline, robot_online
from nova_vda5050.schemas.factsheet import MobileRobotActionDef
from nova_vda5050.schemas.instant_actions import InstantActionsMessage
from nova_vda5050.schemas.order import OrderMessage
from nova_vda5050.transform import transform_telemetry_to_visualization

logger = logging.getLogger(__name__)


# Callback signature for translated VDA5050 commands.  The bridge does
# the JSON parsing + Pydantic validation; the adapter decides how to
# dispatch each ``(action_type, params)`` pair to the driver.
InstantActionCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]
OrderCallback = Callable[[str, OrderMessage], Awaitable[None]]


class VDA5050Bridge:
    """Manages VDA5050 message publishing and inbound command handling.

    Owns no telemetry polling of its own — the adapter is expected to
    drive :meth:`publish_state` / :meth:`publish_visualization` from the
    SDK driver's existing telemetry sampler so VDA5050 sees exactly the
    same snapshot the SDK telemetry pump sees.
    """

    def __init__(self, nats_client: NATS, nats_prefix: str = "rt.v1"):
        self._nc = nats_client
        self._mapper = TopicMapper(nats_prefix)
        self._header_counters: dict[str, int] = {}
        self._on_instant_action: InstantActionCallback | None = None
        self._on_order: OrderCallback | None = None

    def _next_header(self, key: str) -> int:
        self._header_counters[key] = self._header_counters.get(key, 0) + 1
        return self._header_counters[key]

    # ── Outbound: publish VDA5050 messages ───────────────────────────────

    async def publish_state(
        self, robot_model: str, robot_id: str, telemetry: dict[str, Any]
    ) -> None:
        """Publish a VDA5050 state message from Nova telemetry."""
        manufacturer = get_manufacturer(robot_model)
        header_id = self._next_header(f"state.{robot_id}")
        state_msg = transform_telemetry_to_state(
            telemetry, robot_id, manufacturer, header_id
        )
        subject = self._mapper.nova_state_to_vda5050_nats(robot_model, robot_id)
        await self._publish(subject, state_msg.model_dump())

    async def publish_visualization(
        self, robot_model: str, robot_id: str, telemetry: dict[str, Any]
    ) -> None:
        """Publish a VDA5050 visualization message (high-rate position)."""
        manufacturer = get_manufacturer(robot_model)
        header_id = self._next_header(f"viz.{robot_id}")
        viz_msg = transform_telemetry_to_visualization(
            telemetry, robot_id, manufacturer, header_id
        )
        subject = self._mapper.nova_state_to_vda5050_visualization_nats(
            robot_model, robot_id
        )
        await self._publish(subject, viz_msg.model_dump())

    async def publish_online(self, robot_model: str, robot_id: str) -> None:
        msg = robot_online(robot_model, robot_id)
        subject = self._mapper.nova_connection_nats(robot_model, robot_id)
        await self._publish(subject, msg.model_dump())
        logger.info("[vda5050] Published ONLINE for %s/%s", robot_model, robot_id)

    async def publish_offline(self, robot_model: str, robot_id: str) -> None:
        msg = robot_offline(robot_model, robot_id)
        subject = self._mapper.nova_connection_nats(robot_model, robot_id)
        await self._publish(subject, msg.model_dump())
        logger.info("[vda5050] Published OFFLINE for %s/%s", robot_model, robot_id)

    async def publish_factsheet(
        self,
        robot_model: str,
        robot_id: str,
        actions: list[MobileRobotActionDef],
    ) -> None:
        """Publish a factsheet with the provided action definitions.

        The bridge is action-set agnostic; the adapter supplies the
        PiDog-specific list (built once at startup).
        """
        msg = build_factsheet(robot_model, robot_id, actions)
        subject = self._mapper.nova_factsheet_nats(robot_model, robot_id)
        await self._publish(subject, msg.model_dump(by_alias=True))
        logger.info("[vda5050] Published factsheet for %s/%s", robot_model, robot_id)

    # ── Inbound: subscribe to VDA5050 orders/instantActions ──────────────

    async def start_inbound_subscriptions(
        self,
        on_instant_action: InstantActionCallback | None = None,
        on_order: OrderCallback | None = None,
    ) -> None:
        """Subscribe to VDA5050 order and instantActions subjects.

        Parameters
        ----------
        on_instant_action:
            ``async (robot_id, action_type, action_params) -> None`` —
            invoked once per action in an InstantActionsMessage.  We pass
            the raw action type (not a Nova-translated command) because
            PiDog's action vocabulary is much richer than Nova's standard
            ``{enable, move, stop, disable}`` set and needs custom
            dispatch.
        on_order:
            ``async (robot_id, OrderMessage) -> None`` — invoked once per
            inbound order.  PiDog doesn't currently execute orders, so
            the adapter just logs and acks.
        """
        self._on_instant_action = on_instant_action
        self._on_order = on_order
        subject = self._mapper.vda5050_inbound_wildcard()
        await self._nc.subscribe(subject, cb=self._handle_vda5050_inbound)
        logger.info("[vda5050] Subscribed to inbound VDA5050: %s", subject)

    async def _handle_vda5050_inbound(self, msg) -> None:
        addr = self._mapper.parse_vda5050_nats_subject(msg.subject)
        if addr is None:
            return

        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except Exception:
            logger.warning("[vda5050] Invalid JSON on %s", msg.subject)
            return

        if addr.message_type == "instantActions":
            await self._dispatch_instant_actions(addr.serial_number, payload)
        elif addr.message_type == "order":
            await self._dispatch_order(addr.serial_number, payload)

    async def _dispatch_instant_actions(self, serial_number: str, payload: dict) -> None:
        try:
            msg = InstantActionsMessage.model_validate(payload)
        except Exception as exc:
            logger.warning("[vda5050] Invalid instantActions: %s", exc)
            return

        if self._on_instant_action is None:
            return

        for action in msg.actions:
            params: dict[str, Any] = {}
            for p in action.actionParameters or []:
                params[p.key] = p.value
            try:
                await self._on_instant_action(serial_number, action.actionType, params)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[vda5050] instantAction %s failed: %s",
                    action.actionType,
                    exc,
                )

    async def _dispatch_order(self, serial_number: str, payload: dict) -> None:
        try:
            msg = OrderMessage.model_validate(payload)
        except Exception as exc:
            logger.warning("[vda5050] Invalid order: %s", exc)
            return

        if self._on_order is None:
            return

        try:
            await self._on_order(serial_number, msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[vda5050] order dispatch failed: %s", exc)

    # ── Internal ─────────────────────────────────────────────────────────

    async def _publish(self, subject: str, payload: dict) -> None:
        if self._nc is None or not self._nc.is_connected:
            return
        try:
            await self._nc.publish(subject, json.dumps(payload).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[vda5050] Publish failed on %s: %s", subject, exc)


__all__ = ["VDA5050Bridge"]
