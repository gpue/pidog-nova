"""pidog-nova entry point — wires the SDK app to :class:`PidogDriver`.

Phase 1 of the migration runs the SDK in **pure-HTTP mode**: we pass
``nats=None`` to :func:`create_connector_app`, which short-circuits the
SDK lifespan (no NATS broker connection, no ControlRuntime, no
TelemetryPublisher, no RegistryPublisher).

Phase 2a will swap to ``nats=NatsConnection(...)`` and wire ControlRuntime
+ TelemetryPublisher + RegistryPublisher in the same shape as
``unitree-nova/src/unitree_nova/main.py``.
"""

from __future__ import annotations

from mobile_integration_sdk import create_connector_app

from pidog_nova._utils import logger
from pidog_nova.driver import PidogDriver
from pidog_nova.settings import settings as pidog_settings
from pidog_nova.settings import to_connector_settings

# ─── build SDK components ─────────────────────────────────────────────────

_connector_settings = to_connector_settings(pidog_settings)
_driver = PidogDriver()


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


async def _on_shutdown() -> None:
    logger.info("pidog-nova shutting down...")
    await _driver.shutdown()
    logger.info("pidog-nova shutdown complete")


# ─── build the FastAPI app ────────────────────────────────────────────────

app = create_connector_app(
    driver=_driver,
    settings=_connector_settings,
    # Phase 1: pure-HTTP mode. Phase 2a will pass NatsConnection() here.
    nats=None,
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
