# Pidog Nova

Nova-compatible connector for SunFounder PiDog robots. Implemented on top of
the [`mobile-integration-sdk`](https://github.com/wandelbotsgmbh/mobile-integration-sdk)
so the HTTP surface, NATS realtime subjects, and registry layout match every
other Nova mobile-robot driver (`spot-nova`, `unitree-nova`, `sim-connector`).

## Runtime at a glance

| | |
|--|--|
| Language       | Python 3.11+                                                            |
| Framework      | FastAPI (HTTP) + nats-py (realtime) + httpx (upstream proxy)            |
| Build / deps   | `uv` (lockfile-driven; reproducible via `uv sync --frozen`)              |
| Entry point    | `pidog_nova.main:app`                                                   |
| Default port   | `8000`                                                                  |
| Image          | `waboreg.azurecr.io/pidog-nova:<short-sha>` (built by GitHub Actions)   |

The connector itself owns no robot logic: it forwards every action / camera /
control request to an upstream **PiDog controller-control daemon** running on
the Raspberry Pi inside the dog. This service is the Nova-facing protocol
adapter — REST + NATS in, HTTP in to the dog.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          pidog-nova process                          │
│                                                                      │
│  FastAPI app  ←─  create_connector_app(driver=PidogDriver, ...)      │
│                                                                      │
│  ┌────────────────────────┐    ┌──────────────────────────────┐      │
│  │ mobile-integration-sdk │    │  PidogDriver  (this repo)    │      │
│  │ ────────────────────── │    │ ──────────────────────────── │      │
│  │  /health   /config     │    │  start / shutdown            │      │
│  │  /capabilities         │    │  snapshot_state  (telemetry) │      │
│  │  /actions/{name}       │    │  execute_action  / stop      │      │
│  │  /cameras/...          │    │  recover                     │      │
│  │  ControlRuntime        │◄───│                              │      │
│  │  TelemetryPublisher    │    └──────────────┬───────────────┘      │
│  │  RegistryPublisher     │                   │                      │
│  │  GraphNavBridge (opt)  │                   │                      │
│  └────────────────────────┘    ┌──────────────▼───────────────┐      │
│                                │  PidogClient (this repo)     │      │
│  ┌────────────────────────┐    │ ──────────────────────────── │      │
│  │ Vda5050Adapter (here)  │    │  bounded RequestScheduler    │      │
│  │  ────────────────────  │    │  emergency_stop / actions    │      │
│  │  state / viz / connect │    │  battery / debug/state polls │      │
│  │  factsheet (22 actions)│    └──────────────┬───────────────┘      │
│  │  inbound order/instant │                   │                      │
│  └────────────────────────┘                   │                      │
│                                               │                      │
└───────────────────────────────────────────────┼──────────────────────┘
                                                │ HTTP
                                                ▼
                              ┌───────────────────────────────────┐
                              │  PiDog controller-control daemon  │
                              │  (Raspberry Pi inside the dog)    │
                              └───────────────────────────────────┘
```

What lives where:

- **`src/pidog_nova/main.py`** – wires the SDK + driver + adapters. Mounts
  the connector FastAPI app, the operator UI (`/ui`), and brand assets
  (`/static`).
- **`src/pidog_nova/driver.py`** – `PidogDriver`, implementing the SDK driver
  protocol. Exposes `snapshot_state` (async, polls `/battery` + `/debug/state`
  concurrently), `execute_action`, `stop` (motion-stop → upstream `POST /stop`),
  and `recover` (best-effort stand-up).
- **`src/pidog_nova/pidog_client.py`** – thin async client against the upstream
  daemon, with a bounded `RequestScheduler` so a slow Pi can't backlog us.
- **`src/pidog_nova/camera_hub.py`** – `CameraStreamHub` that multiplexes MJPEG
  snapshots from the upstream camera endpoint.
- **`src/pidog_nova/vda5050_bridge.py`** + **`vda5050_adapter.py`** – VDA5050
  protocol layer. Reuses the SDK's NATS connection and the driver's existing
  sampler (no duplicate upstream polling).
- **`src/pidog_nova/config_store.py`** – Nova Object Store client for
  persisting the upstream PiDog IP/port across pod restarts. Dual-writes to a
  local JSON file as a dev/offline fallback.
- **`src/pidog_nova/settings.py`** – pydantic-settings layer that reads env
  vars and promotes persisted config over defaults.

The legacy `app/` package (FastAPI gateway hand-rolled before the SDK existed)
has been removed; see commit `Phase 4 …` for the migration write-up.

## Capabilities

PiDog has no continuous-velocity / odometry / SLAM / GraphNav surface, so the
connector advertises a deliberately narrow capability set:

| Capability     | Status   | Notes                                       |
|----------------|----------|---------------------------------------------|
| `CAMERAS`      | ✅       | Single feed (`main`), MJPEG snapshots       |
| `RECOVER`      | ✅       | Best-effort: triggers `stand` upstream      |
| `WALK_CONTROL` | ❌       | No continuous-velocity API on the upstream  |
| `ACTIONS`      | implicit | Auto-mounted by the SDK actions router      |
| `CONFIG`       | implicit | Always-on (POST `/config` for upstream IP)  |

Realtime `control.cmd` messages over NATS work, but `move` returns a
`not_implemented` error event. `stop` and `disable` both fall through to the
motion-stop convention (`driver.stop()` → upstream `POST /stop`).

## API surface

The spec is owned by the SDK and bundled in
`mobile_integration_sdk.spec.robot-connector.yaml`. The connector serves the
spec at `/openapi.json` and `/openapi.yaml`.

| Path                                                  | Notes                            |
|-------------------------------------------------------|----------------------------------|
| `/health`                                             | Always 200 while the process is up |
| `/config`  GET / POST                                 | Read / update the upstream PiDog IP |
| `/api/capabilities`                                   | Enabled capability set            |
| `/api/{robot_model}/{robot_id}/actions`               | List + execute pre-canned actions |
| `/api/{robot_model}/{robot_id}/recover`               | Best-effort recover               |
| `/api/{robot_model}/{robot_id}/cameras/...`           | MJPEG snapshot + stream           |
| `/api/{robot_model}/{robot_id}/control/...`           | Realtime control over NATS        |
| `/openapi.json` / `/openapi.yaml`                     | Bundled spec                      |
| `/ui/` / `/static/`                                   | Operator HTML page + brand assets |

NATS subjects (default prefix `rt.v1`):

- `rt.v1.robot.pidog.pidog.control.cmd` – inbound commands
- `rt.v1.robot.pidog.pidog.control.evt` – ack / error events
- `rt.v1.robot.pidog.pidog.telemetry.state` – battery, voltage, charging, state, driving
- `vda5050.v3.SunFounder.pidog.{connection,factsheet,state,visualization,order,instantActions}`

## Configuration

| Env var                       | Default                          | Purpose                                  |
|-------------------------------|----------------------------------|------------------------------------------|
| `BASE_PATH`                   | empty                            | Set to `/cell/pidog-nova` behind ingress |
| `ROBOT_MODEL`                 | `pidog`                          | KV registry key prefix                   |
| `ROBOT_ID`                    | `pidog`                          | KV registry key                          |
| `PIDOG_IP` / `PIDOG_PORT`     | empty / `8000`                   | Upstream PiDog daemon target             |
| `PIDOG_PROXY_MAX_CONCURRENCY` | `2`                              | Bounded request scheduler                |
| `PIDOG_PROXY_QUEUE_SIZE`      | `64`                             | Request scheduler queue cap              |
| `PIDOG_CAMERA_POLL_INTERVAL_S`| `0.2`                            | MJPEG sample rate                        |
| `NATS_URL`                    | `nats://localhost:4222`          | Realtime broker                          |
| `NATS_SUBJECT_PREFIX`         | `rt.v1`                          | NATS subject root                        |
| `REGISTRY_ROBOTS_BUCKET`      | `registry_robots`                | KV bucket for robot discovery            |
| `CONNECTOR_BASE_URL`          | `http://localhost:8000`          | URL advertised in KV `endpoints`         |
| `CONFIG_FILE`                 | `/data/config.json`              | Local-JSON dual-write fallback           |
| `NOVA_API_GATEWAY`            | `…wandelbots.svc.cluster.local…` | Nova Object Store base                   |

## Local development

```bash
# Install deps (locked) and the project itself in editable mode.
uv sync

# Run a local NATS broker (one-shot Docker container).
docker run -d --rm --name nats -p 4222:4222 -p 9222:9222 nats:2.10-alpine -js -ws

# Boot the connector (no upstream PiDog required — degrades gracefully).
PIDOG_IP=127.0.0.1 PIDOG_PORT=19999 uv run uvicorn pidog_nova.main:app --reload

# Probe.
curl http://localhost:8000/health
curl http://localhost:8000/api/capabilities
curl http://localhost:8000/openapi.json | jq '.info'
```

With no upstream daemon, telemetry samples return `None`, the KV registry
marks the robot `online=false` after three missed heartbeats, and any
`control.cmd` other than `stop`/`disable` returns an error event. Once
`PIDOG_IP` resolves to a live daemon, the registry flips to `online=true`
and full motion / camera / VDA5050 traffic resumes.

## Build

```bash
docker build -t pidog-nova:dev .
docker run -p 8000:8000 \
  -e PIDOG_IP=192.168.1.50 \
  -e NATS_URL=nats://host.docker.internal:4222 \
  pidog-nova:dev
```

`Dockerfile` uses `uv sync --frozen --no-dev` so the produced image matches
`uv.lock` exactly. A `GITHUB_TOKEN` build-arg is required when any
dependency lives in a private wandelbotsgmbh repo.

> **Note on the build-arg:** the token is unset from `~/.gitconfig` at the
> end of the sync step, but Docker still records the `ARG GITHUB_TOKEN`
> value in the layer history (visible via `docker history --no-trunc`).
> Treat the produced image as containing the build credential and only
> push it to a trusted private registry (e.g. `waboreg.azurecr.io`).
> Migrating to BuildKit `--mount=type=secret` is tracked as a follow-up.

## Deployment

Deployment is driven by `nova-deploy` and the manifests in
`nova-deploy/manifests/templates/pidog-nova/`. The CI/CD flow:

1. Push to `main` → GitHub Actions builds + tags the image with the short SHA
   and pushes to `waboreg.azurecr.io/pidog-nova`.
2. From `nova-deploy`, run `make sync-tags` to pick up the latest green
   build, then `make deploy` to render manifests and apply.

For a manual K8s deploy, image tags are visible via
`make report` and updates can be applied with `make deploy`.

## Related platform docs

- API overview: `/cell/formfactors-docs/api/robot-connector`
- AsyncAPI / realtime: `/cell/formfactors-docs/realtime/robot-connector-async`
- NATS subject reference: `formfactors-docs/content/guides/nats-subjects.md`
- KV registry schema: `formfactors-docs/content/guides/nats-kv-registry.md`
- Robot capability matrix: `formfactors-docs/content/guides/robot-capabilities.md`
- Design language (used by `/ui/index.html`): `formfactors-docs/content/guides/design-language.md`
