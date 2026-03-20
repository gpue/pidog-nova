from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class ProxyQueueFullError(Exception):
    pass


class UpstreamUnavailableError(Exception):
    pass


class UpstreamTimeoutError(Exception):
    pass


@dataclass(slots=True)
class ProxyResult:
    status_code: int
    headers: dict[str, str]
    content: bytes


@dataclass(slots=True)
class ProxyJob:
    method: str
    url: str
    headers: dict[str, str]
    content: bytes | None
    future: asyncio.Future[ProxyResult]


def sanitize_forward_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }


def extract_jpeg_frames(buffer: bytearray) -> list[bytes]:
    frames: list[bytes] = []
    search_from = 0

    while True:
        start = buffer.find(_JPEG_SOI, search_from)
        if start < 0:
            if len(buffer) > 1024 * 1024:
                del buffer[:-1024]
            return frames

        end = buffer.find(_JPEG_EOI, start + 2)
        if end < 0:
            if start > 0:
                del buffer[:start]
            return frames

        end += len(_JPEG_EOI)
        frames.append(bytes(buffer[start:end]))
        del buffer[:end]
        search_from = 0


class RequestScheduler:
    def __init__(
        self,
        *,
        max_concurrency: int = 2,
        max_queue_size: int = 64,
        request_timeout: float = 30.0,
    ) -> None:
        self._max_concurrency = max(1, max_concurrency)
        self._request_timeout = request_timeout
        self._queue: asyncio.Queue[ProxyJob] = asyncio.Queue(maxsize=max_queue_size)
        self._start_lock = asyncio.Lock()
        self._workers: list[asyncio.Task[None]] = []
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        async with self._start_lock:
            if self._client is not None:
                return
            self._client = httpx.AsyncClient(timeout=self._request_timeout)
            self._workers = [
                asyncio.create_task(
                    self._worker(index), name=f"pidog-proxy-worker-{index}"
                )
                for index in range(self._max_concurrency)
            ]

    async def stop(self) -> None:
        async with self._start_lock:
            workers = self._workers
            self._workers = []
            client = self._client
            self._client = None

        for worker in workers:
            worker.cancel()
        for worker in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker

        while not self._queue.empty():
            job = self._queue.get_nowait()
            if not job.future.done():
                job.future.set_exception(
                    HTTPException(status_code=503, detail="Proxy stopping")
                )
            self._queue.task_done()

        if client is not None:
            await client.aclose()

    async def submit(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> ProxyResult:
        await self.start()

        if self._queue.full():
            raise ProxyQueueFullError("Pidog request queue is full")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ProxyResult] = loop.create_future()
        await self._queue.put(
            ProxyJob(
                method=method,
                url=url,
                headers=sanitize_forward_headers(headers),
                content=content,
                future=future,
            )
        )
        return await future

    async def _worker(self, worker_index: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                result = await self._perform(job)
            except Exception as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
            else:
                if not job.future.done():
                    job.future.set_result(result)
            finally:
                self._queue.task_done()

    async def _perform(self, job: ProxyJob) -> ProxyResult:
        client = self._client
        if client is None:
            raise HTTPException(status_code=503, detail="Proxy unavailable")

        try:
            response = await client.request(
                job.method,
                job.url,
                content=job.content,
                headers=job.headers,
            )
        except httpx.ConnectError as exc:
            raise UpstreamUnavailableError("Pidog unreachable") from exc
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError("Pidog timeout") from exc

        return ProxyResult(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )


class CameraStreamHub:
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

    async def get_snapshot(self, snapshot_url: str) -> bytes | None:
        await self._ensure_stream(snapshot_url)
        if self._latest_frame is not None:
            return self._latest_frame
        try:
            await asyncio.wait_for(
                self._latest_frame_event.wait(), timeout=self._first_frame_timeout
            )
        except asyncio.TimeoutError:
            return None
        return self._latest_frame

    async def stream_response(
        self, request: Request, snapshot_url: str
    ) -> StreamingResponse:
        queue = await self._subscribe(snapshot_url)

        async def stream_bytes():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=30.0)
                    except asyncio.TimeoutError:
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

    async def _subscribe(self, snapshot_url: str) -> asyncio.Queue[bytes]:
        await self._ensure_stream(snapshot_url)
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._subscribers.add(queue)
            latest = self._latest_frame
        if latest is not None:
            queue.put_nowait(latest)
        return queue

    async def _unsubscribe(self, queue: asyncio.Queue[bytes]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def _ensure_stream(self, snapshot_url: str) -> None:
        await self.start()
        async with self._lock:
            if (
                self._source_url == snapshot_url
                and self._task is not None
                and not self._task.done()
            ):
                return
            if self._task is not None:
                self._task.cancel()
            self._source_url = snapshot_url
            self._latest_frame = None
            self._latest_frame_event = asyncio.Event()
            self._task = asyncio.create_task(
                self._run_stream(snapshot_url), name="pidog-camera-hub"
            )

    async def _run_stream(self, snapshot_url: str) -> None:
        while True:
            client = self._client
            if client is None:
                return
            try:
                response = await client.get(snapshot_url)
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
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(frame)


def proxy_result_to_response(result: ProxyResult) -> Response:
    media_type = result.headers.get("content-type", "application/json")
    return Response(
        content=result.content,
        status_code=result.status_code,
        media_type=media_type,
    )
