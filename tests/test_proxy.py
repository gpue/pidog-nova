from __future__ import annotations

import asyncio

import pytest

from app.proxy import (
    CameraStreamHub,
    ProxyQueueFullError,
    RequestScheduler,
    extract_jpeg_frames,
    normalize_stream_source_url,
)


JPEG_ONE = b"\xff\xd8one\xff\xd9"
JPEG_TWO = b"\xff\xd8two\xff\xd9"


def test_extract_jpeg_frames_from_buffer() -> None:
    payload = b"noise" + JPEG_ONE + b"gap" + JPEG_TWO + b"tail"
    buffer = bytearray(payload)

    frames = extract_jpeg_frames(buffer)

    assert frames == [JPEG_ONE, JPEG_TWO]
    assert bytes(buffer) == b"tail"


def test_normalize_stream_source_url_strips_cache_busters() -> None:
    assert normalize_stream_source_url("http://example.com/snapshot?ts=1") == (
        "http://example.com/snapshot"
    )
    assert (
        normalize_stream_source_url("http://example.com/snapshot?foo=1&ts=2&bar=3&_=4")
        == "http://example.com/snapshot?bar=3&foo=1"
    )


@pytest.mark.asyncio
async def test_request_scheduler_limits_queue() -> None:
    scheduler = RequestScheduler(max_concurrency=1, max_queue_size=1)
    await scheduler.start()

    gate = asyncio.Event()

    async def fake_perform(job):
        await gate.wait()
        return None

    scheduler._perform = fake_perform  # type: ignore[method-assign]

    task = asyncio.create_task(scheduler.submit("GET", "http://example.com/one"))
    await asyncio.sleep(0)

    with pytest.raises(ProxyQueueFullError):
        await scheduler.submit("GET", "http://example.com/two")

    gate.set()
    await task
    await scheduler.stop()


@pytest.mark.asyncio
async def test_camera_stream_hub_fans_out_latest_frame() -> None:
    hub = CameraStreamHub()
    await hub.start()

    frames = [JPEG_ONE, JPEG_TWO]

    async def fake_run_stream(stream_url: str) -> None:
        for frame in frames:
            await hub._publish_frame(frame)
        await asyncio.sleep(3600)

    hub._run_stream = fake_run_stream  # type: ignore[method-assign]

    snapshot = await hub.get_snapshot("http://example.com/snapshot")
    assert snapshot in frames

    queue_one = await hub._subscribe("http://example.com/snapshot")
    queue_two = await hub._subscribe("http://example.com/snapshot")

    frame_one = await asyncio.wait_for(queue_one.get(), timeout=1)
    frame_two = await asyncio.wait_for(queue_two.get(), timeout=1)

    assert frame_one in frames
    assert frame_two in frames

    await hub._unsubscribe(queue_one)
    await hub._unsubscribe(queue_two)
    await hub.stop()


@pytest.mark.asyncio
async def test_camera_stream_hub_does_not_restart_for_cache_busted_snapshot_urls() -> (
    None
):
    hub = CameraStreamHub(first_frame_timeout=0.2)
    await hub.start()

    run_urls: list[str] = []

    async def fake_run_stream(stream_url: str) -> None:
        run_urls.append(stream_url)
        await hub._publish_frame(JPEG_ONE)
        await asyncio.sleep(3600)

    hub._run_stream = fake_run_stream  # type: ignore[method-assign]

    snapshot_one = await hub.get_snapshot("http://example.com/snapshot?ts=1")
    assert snapshot_one == JPEG_ONE

    queue = await hub._subscribe("http://example.com/snapshot")
    frame = await asyncio.wait_for(queue.get(), timeout=1)
    assert frame == JPEG_ONE

    snapshot_two = await hub.get_snapshot("http://example.com/snapshot?ts=2")
    assert snapshot_two == JPEG_ONE

    assert run_urls == ["http://example.com/snapshot"]

    await hub._unsubscribe(queue)
    await hub.stop()


@pytest.mark.asyncio
async def test_camera_stream_hub_keeps_subscriber_alive_during_snapshot_refreshes() -> (
    None
):
    hub = CameraStreamHub(first_frame_timeout=0.2)
    await hub.start()

    run_urls: list[str] = []
    published = 0

    async def fake_run_stream(stream_url: str) -> None:
        nonlocal published
        run_urls.append(stream_url)
        while True:
            published += 1
            await hub._publish_frame(f"frame-{published}".encode())
            await asyncio.sleep(0.02)

    hub._run_stream = fake_run_stream  # type: ignore[method-assign]

    await hub.get_snapshot("http://example.com/snapshot?ts=1")
    queue = await hub._subscribe("http://example.com/snapshot")

    received: list[bytes] = []
    for refresh in range(2, 5):
        await hub.get_snapshot(f"http://example.com/snapshot?ts={refresh}")
        received.append(await asyncio.wait_for(queue.get(), timeout=1))

    assert len(received) == 3
    assert len(set(received)) >= 2
    assert run_urls == ["http://example.com/snapshot"]

    await hub._unsubscribe(queue)
    await hub.stop()


@pytest.mark.asyncio
async def test_camera_stream_hub_subscribe_keeps_latest_frame_when_queue_is_refreshed() -> (
    None
):
    hub = CameraStreamHub()
    await hub.start()

    async def fake_run_stream(stream_url: str) -> None:
        await hub._publish_frame(JPEG_ONE)
        await asyncio.sleep(0)
        await hub._publish_frame(JPEG_TWO)
        await asyncio.sleep(3600)

    hub._run_stream = fake_run_stream  # type: ignore[method-assign]

    queue = await hub._subscribe("http://example.com/snapshot?ts=1")
    frame = await asyncio.wait_for(queue.get(), timeout=1)

    assert frame in {JPEG_ONE, JPEG_TWO}

    await hub._unsubscribe(queue)
    await hub.stop()
