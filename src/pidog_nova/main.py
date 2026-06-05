"""pidog-nova entry point — wires the SDK app to :class:`PidogDriver`.

Phase 2a–3 stack:

* :class:`mobile_integration_sdk.NatsConnection` — shared NATS client.
* :class:`mobile_integration_sdk.ControlRuntime` — subscribes to
  ``…control.cmd`` and dispatches to driver hooks
  (``stop`` → motion-stop via upstream ``POST /stop``; ``move`` returns
  ``not_implemented`` since PiDog has no continuous-velocity surface).
* :class:`mobile_integration_sdk.TelemetryPublisher` — sample/publish loop
  driven by :meth:`PidogDriver.snapshot_state`.
* :class:`mobile_integration_sdk.RegistryPublisher` — per-robot KV
  publisher with TTL + heartbeat in ``registry_robots``.
* :class:`pidog_nova.vda5050_adapter.Vda5050Adapter` — VDA5050 bridge
  reusing the SDK NATS client and the driver's sampler so VDA5050 sees
  the same upstream snapshot as the SDK telemetry pump (no duplicate
  polling of the PiDog daemon).

The HTTP surface (REST routes, capability gates, OpenAPI bundled spec) is
fully owned by :func:`mobile_integration_sdk.create_connector_app`.
"""

from __future__ import annotations

from mobile_integration_sdk import (
    ControlRuntime,
    NatsConnection,
    RegistryPublisher,
    Subjects,
    TelemetryPublisher,
    create_connector_app,
    infer_capabilities,
)

from pidog_nova._utils import logger
from pidog_nova.driver import PidogDriver
from pidog_nova.settings import settings as pidog_settings
from pidog_nova.settings import to_connector_settings
from pidog_nova.vda5050_adapter import Vda5050Adapter

# ─── build SDK components (lifespan starts/stops them) ────────────────────

_connector_settings = to_connector_settings(pidog_settings)
_driver = PidogDriver()

_nats = NatsConnection(url=_connector_settings.nats_url, name="pidog-nova")
_subjects = Subjects(
    prefix=_connector_settings.nats_subject_prefix,
    robot_model=_connector_settings.robot_model,
    robot_id=_connector_settings.robot_id,
)

# SDK v0.3 (opt #1): NatsConnection now delegates publish/subscribe/etc.
# to its primary client, so ControlRuntime + TelemetryPublisher can take
# the wrapper directly — no post-connect rebinding required.
_control_runtime = ControlRuntime(_nats, _subjects, _driver)

_telemetry = TelemetryPublisher(_nats, _subjects)
_telemetry.add("state", _driver.snapshot_state)

_registry = RegistryPublisher(
    nats=_nats,
    robot_id=_connector_settings.robot_id,
    robot_model=_connector_settings.robot_model,
    source=_connector_settings.source,
    connector_base_url=_connector_settings.connector_base_url,
    base_path=_connector_settings.base_path,
    nats_subject_prefix=_connector_settings.nats_subject_prefix,
    capabilities=infer_capabilities(_driver),
    # KV ``online`` flag tracks upstream-daemon reachability, not just
    # whether this connector process is up. The driver's telemetry
    # sampler refreshes ``_upstream_alive`` every tick (0.5 s by default),
    # so the registry's 15 s heartbeat always sees a recent value.
    health_check=lambda: _driver.upstream_alive,
    heartbeat_interval_s=_connector_settings.registry_heartbeat_s,
    robots_bucket=_connector_settings.robots_bucket,
)

# VDA5050 adapter is constructed eagerly; its ``start()`` is invoked
# from ``_on_startup`` once NATS is connected so it can grab the live
# primary client.  The adapter shares the SDK's NATS client and the
# driver's existing sampler — no duplicate upstream polling.
_vda5050: Vda5050Adapter | None = None


# ─── startup / shutdown hooks ─────────────────────────────────────────────


async def _on_startup() -> None:
    global _vda5050

    logger.info(
        "pidog-nova v%s starting (model=%s, id=%s, upstream=%s)",
        pidog_settings.api_version,
        pidog_settings.robot_model,
        pidog_settings.robot_id,
        pidog_settings.pidog_base_url or "<unconfigured>",
    )
    await _driver.start()

    # Connect NATS now so the VDA5050 adapter can subscribe / publish from
    # its own start(). The SDK lifespan will call connect() again later —
    # it's a no-op when already connected.
    await _nats.connect()

    # VDA5050 adapter is auxiliary — a failure here must not take down the
    # connector, so we trap and continue without it.
    try:
        _vda5050 = Vda5050Adapter(nc=_nats, driver=_driver)
        await _vda5050.start()
    except Exception:  # noqa: BLE001
        logger.exception("VDA5050 adapter failed to start; continuing without it")
        _vda5050 = None


async def _on_shutdown() -> None:
    logger.info("pidog-nova shutting down...")
    if _vda5050 is not None:
        try:
            await _vda5050.stop()
        except Exception:
            logger.exception("vda5050 adapter shutdown raised")
    try:
        await _driver.shutdown()
    except Exception:
        logger.exception("driver shutdown raised")
    logger.info("pidog-nova shutdown complete")


# ─── build the FastAPI app ────────────────────────────────────────────────

app = create_connector_app(
    driver=_driver,
    settings=_connector_settings,
    nats=_nats,
    registry=_registry,
    control_runtime=_control_runtime,
    telemetry=_telemetry,
    on_startup=_on_startup,
    on_shutdown=_on_shutdown,
    title="Pidog Nova Gateway",
    # SDK v0.3 (opt #5): hand the SDK both static directories so it can
    # mount /ui and /static itself, skipping silently when a dir doesn't
    # exist. The pre-v0.3 manual app.mount(...) block is gone.
    ui_dir=pidog_settings.ui_directory,
    static_dir=pidog_settings.static_directory,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "pidog_nova.main:app",
        host=pidog_settings.host,
        port=pidog_settings.port,
        reload=pidog_settings.reload,
    )
