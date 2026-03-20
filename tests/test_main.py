from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from app import main as pidog_main


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(pidog_main.settings, "pidog_ip", "127.0.0.1")
    monkeypatch.setattr(pidog_main.settings, "pidog_port", 8000)
    return TestClient(pidog_main.app)


def test_camera_video_feed_uses_stream_hub(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: dict[str, object] = {}

    async def fake_proxy_stream(request, url):
        called["url"] = url
        from fastapi.responses import Response

        return Response(
            content=b"stream", media_type="multipart/x-mixed-replace; boundary=frame"
        )

    async def fail_proxy_request(*args, **kwargs):
        raise AssertionError("camera video_feed should not use generic request proxy")

    monkeypatch.setattr(pidog_main, "_proxy_stream", fake_proxy_stream)
    monkeypatch.setattr(pidog_main, "_proxy_request", fail_proxy_request)

    response = client.get(f"{pidog_main.API_PREFIX}/camera/pidog/main/video_feed")

    assert response.status_code == 200
    assert response.content == b"stream"
    assert (
        called["url"]
        == f"{pidog_main.settings.pidog_base_url}/api/camera/pidog/main/video_feed"
    )


def test_camera_snapshot_uses_camera_hub(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: dict[str, object] = {}

    async def fake_get_snapshot(url):
        called["url"] = url
        return b"jpeg-bytes"

    async def fail_proxy_request(*args, **kwargs):
        raise AssertionError("camera snapshot should not use generic request proxy")

    monkeypatch.setattr(pidog_main.camera_stream_hub, "get_snapshot", fake_get_snapshot)
    monkeypatch.setattr(pidog_main, "_proxy_request", fail_proxy_request)

    response = client.get(f"{pidog_main.API_PREFIX}/camera/pidog/main/snapshot")

    assert response.status_code == 200
    assert response.content == b"jpeg-bytes"
    assert response.headers["content-type"] == "image/jpeg"
    assert (
        called["url"]
        == f"{pidog_main.settings.pidog_base_url}/api/camera/pidog/main/snapshot"
    )


def test_robot_routes_still_use_generic_proxy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: dict[str, object] = {}

    async def fake_proxy_request(request, method, url, content=None):
        called["method"] = method
        called["url"] = url
        from fastapi.responses import Response

        return Response(content=b"ok", media_type="application/json")

    monkeypatch.setattr(pidog_main, "_proxy_request", fake_proxy_request)

    response = client.get(f"{pidog_main.API_PREFIX}/pidog/pidog/walk/forward")

    assert response.status_code == 200
    assert response.content == b"ok"
    assert called["method"] == "GET"
    assert (
        called["url"]
        == f"{pidog_main.settings.pidog_base_url}/api/pidog/pidog/walk/forward"
    )
