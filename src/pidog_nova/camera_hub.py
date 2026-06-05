"""Snapshot-polling MJPEG hub for upstream PiDog cameras.

The PiDog upstream does not expose a native MJPEG stream; it only serves
single-frame JPEG snapshots on
``GET /api/camera/{robot_id}/{feed}/snapshot``.  This module emulates an
MJPEG feed by polling that endpoint at a fixed interval and fanning the
latest frame out to all subscribers as a multipart byte stream.

There is one shared :class:`CameraStreamHub` per process; it lazily switches
its source URL whenever a new ``stream_response()``/``get_snapshot()`` call
arrives with a different URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

_CACHE_BUSTING_QUERY_KEYS = {"ts", "_", "cacheBust", "cache_bust"}


def _with_cache_buster(url: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}ts={time.time_ns()}"


def _normalize_stream_source_url(url: str) -> str:
    """Strip cache-busting query keys so equivalent URLs share a stream task."""
    parts = urlsplit(url)
    if not parts.query and not parts.fragment:
        return url

    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in _CACHE_BUSTING_QUERY_KEYS
    ]
    normalized_query = urlencode(sorted(filtered_query), doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, normalized_query, ""))


class CameraStreamHub:
    """Polls upstream JPEG snapshots and re-publishes as MJPEG to subscribers."""

    def __init__(
        self,
        *,
        connect_timeout: float = 10.0,
        reconnect_delay: float = 1.0,
        first_frame_timeout: float = 3.0,
        poll_interval_s: float = 0.2,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._reconnect_delay = reconnect_delay
        self._first_frame_timeout = first_frame_timeout
        self._poll_interval_s = max(0.05, poll_interval_s)
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._source_url: str | None = None
        self._latest_frame: bytes | None = None
        self._latest_frame_event = asyncio.Event()
        self._subscribers: set[asyncio.Queue[bytes]] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=self._connect_timeout, read=None)
            )

    async def stop(self) -> None:
        async with self._lock:
            task = self._task
            self._task = None
            self._source_url = None
            self._latest_frame = None
            self._latest_frame_event = asyncio.Event()
            self._subscribers.clear()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Public API ────────────────────────────────────────────────────

    async def get_snapshot(self, snapshot_url: str) -> bytes | None:
        """Return the most recent JPEG frame; wait up to ``first_frame_timeout``."""
        await self._ensure_stream(snapshot_url)
        if self._latest_frame is not None:
            return self._latest_frame
        try:
            await asyncio.wait_for(
                self._latest_frame_event.wait(), timeout=self._first_frame_timeout
            )
        except TimeoutError:
            return None
        return self._latest_frame

    async def stream_response(
        self, request: Request, snapshot_url: str
    ) -> StreamingResponse:
        """Return a multipart/x-mixed-replace response of live frames."""
        queue = await self._subscribe(snapshot_url)

        async def stream_bytes():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=30.0)
                    except TimeoutError:
                        continue
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                        + frame
                        + b"\r\n"
                    )
            finally:
                await self._unsubscribe(queue)

        return StreamingResponse(
            stream_bytes(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    # ── Internals ─────────────────────────────────────────────────────

    async def _subscribe(self, snapshot_url: str) -> asyncio.Queue[bytes]:
        await self._ensure_stream(snapshot_url)
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._subscribers.add(queue)
            latest = self._latest_frame
        if latest is not None:
            self._queue_latest_frame(queue, latest)
        return queue

    async def _unsubscribe(self, queue: asyncio.Queue[bytes]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def _ensure_stream(self, snapshot_url: str) -> None:
        await self.start()
        normalized = _normalize_stream_source_url(snapshot_url)
        async with self._lock:
            if (
                self._source_url == normalized
                and self._task is not None
                and not self._task.done()
            ):
                return
            if self._task is not None:
                self._task.cancel()
            self._source_url = normalized
            self._latest_frame = None
            self._latest_frame_event = asyncio.Event()
            self._task = asyncio.create_task(
                self._run_stream(normalized), name="pidog-camera-hub"
            )

    async def _run_stream(self, snapshot_url: str) -> None:
        while True:
            client = self._client
            if client is None:
                return
            try:
                response = await client.get(_with_cache_buster(snapshot_url))
                response.raise_for_status()
                async with self._lock:
                    if self._source_url != snapshot_url:
                        return
                if response.content:
                    await self._publish_frame(response.content)
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                raise
            except httpx.ConnectError:
                logger.warning("Pidog camera snapshot unavailable: %s", snapshot_url)
            except httpx.TimeoutException:
                logger.warning("Pidog camera snapshot timed out: %s", snapshot_url)
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "Pidog camera snapshot returned %s for %s",
                    exc.response.status_code,
                    snapshot_url,
                )
            except Exception:
                logger.exception("Pidog camera snapshot failed: %s", snapshot_url)

            await asyncio.sleep(self._reconnect_delay)

    async def _publish_frame(self, frame: bytes) -> None:
        async with self._lock:
            self._latest_frame = frame
            self._latest_frame_event.set()
            subscribers = list(self._subscribers)

        for queue in subscribers:
            self._queue_latest_frame(queue, frame)

    @staticmethod
    def _queue_latest_frame(queue: asyncio.Queue[bytes], frame: bytes) -> None:
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(frame)
