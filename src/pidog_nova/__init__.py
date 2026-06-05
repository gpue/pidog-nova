"""pidog-nova — Nova-compatible connector for SunFounder PiDog robots.

Phase 1 of the mobile-integration-sdk migration: the canonical REST surface,
NATS subjects, and KV registry entries are owned by ``mobile_integration_sdk``.
This package contributes only the PiDog-specific driver and HTTP gateway to
the upstream ``controller-control`` server.

Legacy ``app/`` package remains in place as a regression reference until
Phase 4, when it will be deleted.
"""

__version__ = "1.0.0"
