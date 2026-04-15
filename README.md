# Pidog Nova

Pidog Nova is a Nova-compatible gateway connector for SunFounder PiDog robots.
It exposes the unified connector REST surface, proxies requests to a PiDog
device, and publishes robot/camera/model discovery data to NATS KV.

## Overview

- Runtime: Python 3.11 with FastAPI + httpx + nats-py
- Entry point: `app/main.py`
- Default port: `8000`
- Role: physical connector and protocol adapter between Nova clients and PiDog

## Architecture and Runtime

- Gateway endpoints are implemented directly for health, config, capabilities,
  and realtime subject metadata (`ws/walk/info`)
- Robot and camera routes are proxied to the configured PiDog upstream
- Registry publishing is handled by `app/registry.py`
- Robot registry entries are persistent (online/offline flag)

## API Surface

- Local base URL: `http://localhost:8000`
- Nova ingress base URL: `/cell/pidog-nova`
- Unified connector routes under `/api/{robot_model}/{robot_id}/...`

Notable endpoints:

- `GET /api/robots`
- `GET /api/cameras`
- `GET /api/capabilities`
- `GET /api/{robot_model}/{robot_id}/ws/walk/info`
- `DELETE /api/{robot_model}/{robot_id}/registry` (decommission)
- catch-all proxy routes for connector API compatibility

For the complete connector contract, see:

- `/cell/formfactors-docs/api/robot-connector`
- `/cell/formfactors-docs/realtime/robot-connector-async`

## Configuration

Core environment variables:

- `BASE_PATH` (set `/cell/pidog-nova` behind ingress)
- `ROBOT_MODEL` (default: `pidog`)
- `ROBOT_ID` (default: `pidog`)
- `PIDOG_IP` and `PIDOG_PORT` (upstream PiDog target)
- `PIDOG_PROXY_MAX_CONCURRENCY` (default: `2`)
- `PIDOG_PROXY_QUEUE_SIZE` (default: `64`)
- `PIDOG_CAMERA_POLL_INTERVAL_S` (default: `0.2`)
- `NATS_URL` / `NATS_BROKER` (default: `nats://localhost:4222`)
- `NATS_WS_URL` (default: `ws://localhost:9222`)
- `NATS_SUBJECT_PREFIX` (default: `rt.v1`)
- `CONNECTOR_BASE_URL`, `REGISTRY_BASE_URL`
- `REGISTRY_ROBOTS_BUCKET`, `REGISTRY_CAMERAS_BUCKET`,
  `REGISTRY_ROBOT_MODELS_BUCKET`

## Local Development

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Deployment Notes

- Exposes port `8000`
- Publishes to:
  - `registry_robots`
  - `registry_cameras`
  - `registry_robot_models`
- On graceful shutdown, robot entry is marked offline; use registry DELETE API
  to remove it permanently

## Troubleshooting

- Connector shows disconnected: verify `PIDOG_IP`/`PIDOG_PORT` reachability
- Robot missing from discovery: verify NATS connectivity and KV bucket access
- Camera stream stalls: inspect upstream PiDog snapshot endpoint behavior

## Related Docs

- API overview: `/cell/formfactors-docs/guides/api-overview`
- Service architecture: `/cell/formfactors-docs/guides/service-architecture`
- NATS subjects: `/cell/formfactors-docs/guides/nats-subjects`
- NATS KV registry: `/cell/formfactors-docs/guides/nats-kv-registry`
- Robot capability matrix: `/cell/formfactors-docs/guides/robot-capabilities`
- Service doc index source: `formfactors-docs/content/guides/service-doc-index.md`
