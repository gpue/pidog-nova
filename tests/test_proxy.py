from __future__ import annotations

import asyncio

import pytest

from app.proxy import (
    CameraStreamHub,
    ProxyQueueFullError,
    RequestScheduler,
    extract_jpeg_frames,
)


JPEG_ONE = b"\xff\xd8one\xff\xd9"
JPEG_TWO = b"\xff\xd8two\xff\xd9"


def test_extract_jpeg_frames_from_buffer() -> None:
    payload = b"noise" + JPEG_ONE + b"gap" + JPEG_TWO + b"tail"
    buffer = bytearray(payload)

    frames = extract_jpeg_frames(buffer)

    assert frames == [JPEG_ONE, JPEG_TWO]
    assert bytes(buffer) == b"tail"


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

    snapshot = await hub.get_snapshot("http://example.com/video_feed")
    assert snapshot in frames

    queue_one = await hub._subscribe("http://example.com/video_feed")
    queue_two = await hub._subscribe("http://example.com/video_feed")

    frame_one = await asyncio.wait_for(queue_one.get(), timeout=1)
    frame_two = await asyncio.wait_for(queue_two.get(), timeout=1)

    assert frame_one in frames
    assert frame_two in frames

    await hub._unsubscribe(queue_one)
    await hub._unsubscribe(queue_two)
    await hub.stop()
