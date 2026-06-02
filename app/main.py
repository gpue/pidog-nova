"""Pidog Nova Gateway - FastAPI app."""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.config import settings
from app.proxy import (
    CameraStreamHub,
    ProxyQueueFullError,
    RequestScheduler,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    proxy_result_to_response,
)
from app.registry import RegistryPublisher
from app.vda5050_bridge import PiDogVDA5050Bridge

logger = logging.getLogger(__name__)

BASE_PATH = (os.getenv("BASE_PATH") or "").strip().rstrip("/")
API_PREFIX = f"{BASE_PATH}/api" if BASE_PATH else "/api"
NATS_WS_URL = os.getenv("NATS_WS_URL", "ws://localhost:9222")
NATS_SUBJECT_PREFIX = os.getenv("NATS_SUBJECT_PREFIX", "rt.v1").strip(".")


def _pidog_healthy() -> bool:
    """Check if Pidog is reachable."""
    if not settings.is_configured:
        return False
    try:
        with httpx.Client(timeout=2.0) as client:
            r = client.get(f"{settings.pidog_base_url}/health")
            return r.status_code == 200
    except Exception:
        return False


registry = RegistryPublisher(
    robot_model=settings.robot_model,
    robot_id=settings.robot_id,
    health_check=_pidog_healthy,
)
request_scheduler = RequestScheduler(
    max_concurrency=settings.proxy_max_concurrency,
    max_queue_size=settings.proxy_queue_size,
)
camera_stream_hub = CameraStreamHub(
    poll_interval_s=settings.camera_poll_interval_s,
)
vda5050_bridge: PiDogVDA5050Bridge | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global vda5050_bridge
    await request_scheduler.start()
    await camera_stream_hub.start()
    await registry.start()
    # Start VDA5050 bridge after registry (needs NATS client)
    if registry._nc is not None and registry._nc.is_connected:
        try:
            vda5050_bridge = PiDogVDA5050Bridge(
                nc=registry._nc,
                robot_model=settings.robot_model,
                robot_id=settings.robot_id,
                pidog_base_url_fn=lambda: settings.pidog_base_url,
                nats_prefix=NATS_SUBJECT_PREFIX,
            )
            await vda5050_bridge.start()
        except Exception:
            logger.warning("VDA5050 bridge failed to start", exc_info=True)
            vda5050_bridge = None
    yield
    if vda5050_bridge is not None:
        try:
            await vda5050_bridge.stop()
        except Exception:
            pass
    await registry.stop()
    await camera_stream_hub.stop()
    await request_scheduler.stop()


app = FastAPI(
    title="Pidog Nova Gateway",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get(f"{BASE_PATH}/health" if BASE_PATH else "/health")
def base_path_health():
    return health()


@app.get(f"{API_PREFIX}/{{robot_model}}/{{robot_id}}/health")
def robot_health(robot_model: str, robot_id: str):
    connected = _pidog_healthy()
    return {
        "status": "healthy" if connected else "degraded",
        "mode": "gateway",
        "robot_connected": connected,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --- Discovery endpoints ---------------------------------------------------


@app.get(f"{API_PREFIX}/robots")
def list_robots():
    """List connected robots managed by this connector."""
    connected = _pidog_healthy()
    return {
        "robots": [
            {
                "robot_id": settings.robot_id,
                "robot_type": settings.robot_model,
                "mode": "gateway",
                "status": "connected" if connected else "disconnected",
            }
        ]
    }


@app.delete(
    f"{API_PREFIX}/{{robot_model}}/{{robot_id}}/registry",
    summary="Permanently remove a robot from the registry",
    description="Decommission a robot by permanently deleting its entry from the "
    "NATS KV registry.  Normal shutdown only marks robots offline; use this "
    "endpoint to fully remove a robot that will not return.",
)
async def delete_robot_registry(robot_model: str, robot_id: str):
    if robot_id != settings.robot_id:
        raise HTTPException(
            status_code=404,
            detail=f"Robot '{robot_id}' is not managed by this connector",
        )
    ok = await registry.unpublish()
    if not ok:
        raise HTTPException(
            status_code=503,
            detail="Failed to remove robot from registry (NATS unavailable)",
        )
    return {"status": "deleted", "robot_id": robot_id}


@app.get(f"{API_PREFIX}/cameras")
def list_cameras():
    """List available camera feeds from the Pidog robot."""
    return {
        "cameras": [
            {
                "robot_id": settings.robot_id,
                "robot_type": settings.robot_model,
                "feed": "main",
                "stream_url": f"{API_PREFIX}/camera/{settings.robot_id}/main/video_feed",
                "snapshot_url": f"{API_PREFIX}/camera/{settings.robot_id}/main/snapshot",
            }
        ]
    }


@app.get(f"{API_PREFIX}/capabilities")
def list_capabilities():
    """List capabilities supported by this connector."""
    return {
        "capabilities": [
            "walk_control",
            "cameras",
            "graphnav",
            "graph_activate",
            "graph_navigate",
        ]
    }


# --- Config (gateway-specific, not proxied) ---
@app.get(f"{API_PREFIX}/config")
def get_config():
    return {
        "robot_model": settings.robot_model,
        "robot_id": settings.robot_id,
        "pidog_ip": settings.pidog_ip or "",
        "pidog_port": settings.pidog_port,
        "connected": _pidog_healthy(),
    }


@app.post(f"{API_PREFIX}/config")
async def update_config(request: Request):
    body = await request.json()
    ip = (body.get("pidog_ip") or "").strip()
    port = body.get("pidog_port")
    if not ip:
        raise HTTPException(status_code=400, detail="pidog_ip required")
    settings.save_config(ip, port)
    return get_config()


@app.post(f"{API_PREFIX}/config/refresh")
def refresh_config():
    """Re-pull configuration from the Nova Object Store."""
    ok = settings.refresh()
    return {"refreshed": ok, **get_config()}


# --- ws/walk/info (gateway implements directly, not proxied) ---
@app.get(f"{API_PREFIX}/{{robot_model}}/{{robot_id}}/ws/walk/info")
def ws_walk_info(robot_model: str, robot_id: str):
    subj_base = f"{NATS_SUBJECT_PREFIX}.robot.{robot_model}.{robot_id}"
    return {
        "protocol": "nats-ws",
        "nats_ws_url": NATS_WS_URL,
        "publish_subjects": [f"{subj_base}.control.cmd"],
        "subscribe_subjects": [f"{subj_base}.control.evt"],
        "telemetry_subject": f"{subj_base}.telemetry.state",
    }


# --- Proxy to Pidog ---
async def _proxy_request(
    request: Request, method: str, url: str, content: bytes | None = None
) -> Response:
    if request.url.query:
        url = f"{url}?{request.url.query}"

    try:
        result = await request_scheduler.submit(
            method,
            url,
            headers=request.headers,
            content=content,
        )
    except ProxyQueueFullError:
        raise HTTPException(status_code=429, detail="Pidog request queue is full")
    except UpstreamUnavailableError:
        raise HTTPException(status_code=503, detail="Pidog unreachable")
    except UpstreamTimeoutError:
        raise HTTPException(status_code=504, detail="Pidog timeout")
    return proxy_result_to_response(result)


async def _proxy_stream(request: Request, url: str) -> Response:
    if request.url.query:
        url = f"{url}?{request.url.query}"

    snapshot_url = url.removesuffix("video_feed") + "snapshot"
    await camera_stream_hub.get_snapshot(snapshot_url)
    return await camera_stream_hub.stream_response(request, snapshot_url)


# Camera proxy
@app.api_route(
    f"{API_PREFIX}/camera/{{robot_id}}/{{path:path}}",
    methods=["GET", "POST"],
)
async def proxy_camera(request: Request, robot_id: str, path: str):
    if not settings.is_configured:
        raise HTTPException(status_code=503, detail="Pidog IP not configured")
    suffix = f"{path}".rstrip("/") if path else ""
    url = f"{settings.pidog_base_url}/api/camera/{robot_id}"
    if suffix:
        url = f"{url}/{suffix}"
    if request.method == "GET" and suffix.endswith("video_feed"):
        return await _proxy_stream(request, url)
    if request.method == "GET" and suffix.endswith("snapshot"):
        snapshot = await camera_stream_hub.get_snapshot(url)
        if snapshot is not None:
            return Response(content=snapshot, media_type="image/jpeg")
    content = await request.body() if request.method == "POST" else None
    return await _proxy_request(request, request.method, url, content)


# --- Recovery endpoint ---
@app.post(f"{API_PREFIX}/{{robot_model}}/{{robot_id}}/recover")
async def recover_robot(request: Request, robot_model: str, robot_id: str):
    """Recover PiDog by commanding 'stand' action.

    PiDog cannot self-right from a physical fall, so this is best-effort.
    Returns 'partial' status since true recovery may require physical intervention.
    """
    if not settings.is_configured:
        raise HTTPException(status_code=503, detail="Pidog IP not configured")

    url = f"{settings.pidog_base_url}/api/{robot_model}/{robot_id}/action/stand"
    try:
        await request_scheduler.submit("POST", url)
        return {
            "status": "partial",
            "steps_completed": ["stand"],
            "steps_failed": ["self_right"],
            "remaining_faults": 0,
            "message": "PiDog cannot self-right physically; legs re-posed via 'stand' action",
        }
    except (UpstreamUnavailableError, UpstreamTimeoutError) as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "steps_completed": [],
                "steps_failed": ["stand"],
                "remaining_faults": 0,
                "message": str(exc),
            },
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "steps_completed": [],
                "steps_failed": ["stand"],
                "remaining_faults": 0,
                "message": str(exc),
            },
        )


# Catch-all proxy for /api/{model}/{id}/*
@app.api_route(
    f"{API_PREFIX}/{{robot_model}}/{{robot_id}}/{{path:path}}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy_robot_api(request: Request, robot_model: str, robot_id: str, path: str):
    if not settings.is_configured:
        raise HTTPException(status_code=503, detail="Pidog IP not configured")
    suffix = f"{path}".rstrip("/") if path else ""
    url = f"{settings.pidog_base_url}/api/{robot_model}/{robot_id}"
    if suffix:
        url = f"{url}/{suffix}"
    content = (
        await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    )
    return await _proxy_request(request, request.method, url, content)


# --- Static ---
@app.get(f"{BASE_PATH}/app_icon.svg" if BASE_PATH else "/app_icon.svg")
def serve_app_icon():
    p = Path(__file__).resolve().parent.parent / "static" / "app_icon.svg"
    if p.exists():
        return FileResponse(p, media_type="image/svg+xml")
    return JSONResponse({"error": "Not found"}, status_code=404)


# --- UI ---
@app.get(f"{API_PREFIX}/ui")
def serve_ui():
    p = UI_DIR / "index.html"
    if p.exists():
        return FileResponse(p, media_type="text/html")
    return JSONResponse({"error": "UI not found"}, status_code=404)


if BASE_PATH:

    @app.get(f"{BASE_PATH}/")
    def serve_base_ui():
        return serve_ui()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
