"""pidog-nova entry point — wires the SDK app to :class:`PidogDriver`.

Phase 2a brings the full realtime stack online:

* :class:`mobile_integration_sdk.NatsConnection` — shared NATS client.
* :class:`mobile_integration_sdk.ControlRuntime` — subscribes to
  ``…control.cmd`` and dispatches to driver hooks
  (``stop`` → motion-stop via upstream ``POST /stop``; ``move`` returns
  ``not_implemented`` since PiDog has no continuous-velocity surface).
* :class:`mobile_integration_sdk.TelemetryPublisher` — sample/publish loop;
  the Phase 2a sampler returns ``None`` (Phase 2b will poll the upstream
  ``/debug/state`` and ``/battery`` endpoints).
* :class:`mobile_integration_sdk.RegistryPublisher` — per-robot KV
  publisher with TTL + heartbeat in ``registry_robots``.

The HTTP surface (REST routes, capability gates, OpenAPI bundled spec) is
fully owned by :func:`mobile_integration_sdk.create_connector_app`.
VDA5050 bridging is intentionally absent here — that's Phase 3.
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

# ─── build SDK components (lifespan starts/stops them) ────────────────────

_connector_settings = to_connector_settings(pidog_settings)
_driver = PidogDriver()

_nats = NatsConnection(url=_connector_settings.nats_url, name="pidog-nova")
_subjects = Subjects(
    prefix=_connector_settings.nats_subject_prefix,
    robot_model=_connector_settings.robot_model,
    robot_id=_connector_settings.robot_id,
)

# ControlRuntime needs a live nats client at start(); we rebind to the
# connected primary in _on_startup below (matches unitree-nova pattern).
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


# ─── startup / shutdown hooks ─────────────────────────────────────────────


async def _on_startup() -> None:
    logger.info(
        "pidog-nova v%s starting (model=%s, id=%s, upstream=%s)",
        pidog_settings.api_version,
        pidog_settings.robot_model,
        pidog_settings.robot_id,
        pidog_settings.pidog_base_url or "<unconfigured>",
    )
    await _driver.start()

    # Connect NATS now so we can rebind ControlRuntime and TelemetryPublisher
    # against the live primary client. The SDK lifespan will call connect()
    # again — it's a no-op when already connected.
    #
    # NOTE: TelemetryPublisher's ``_nc.publish(...)`` call silently fails
    # (logged at DEBUG only) when ``_nc`` is the NatsConnection wrapper
    # rather than the underlying primary client, because the wrapper has
    # no ``publish`` method. Sim-connector passes ``_nats.primary``
    # directly; unitree-nova currently has the same latent bug (filed for
    # cross-cutting cleanup). Until the SDK provides a uniform API, we
    # rebind here.
    await _nats.connect()
    _control_runtime._nc = _nats.primary  # type: ignore[attr-defined]
    _telemetry._nc = _nats.primary  # type: ignore[attr-defined]


async def _on_shutdown() -> None:
    logger.info("pidog-nova shutting down...")
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
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "pidog_nova.main:app",
        host=pidog_settings.host,
        port=pidog_settings.port,
        reload=pidog_settings.reload,
    )
