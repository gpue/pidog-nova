"""Async HTTP client for the upstream PiDog ``controller-control`` server.

Wraps :mod:`httpx` with a bounded RequestScheduler (so a slow Pi doesn't
exhaust connections) and exposes semantic helpers used by
:class:`pidog_nova.driver.PidogDriver`.

The Pi runs a small FastAPI server that exposes:

* ``GET  /health``                              — upstream liveness
* ``POST /stop``                                — emergency stop
* ``POST /api/{model}/{id}/action/{name}``      — execute named action
* ``GET  /api/{model}/{id}/battery``            — battery snapshot
* ``GET  /api/{model}/{id}/debug/state``        — full robot state
* ``GET  /api/camera/{robot_id}/{feed}/snapshot`` — JPEG snapshot
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

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


class ProxyQueueFullError(RuntimeError):
    """Raised when the bounded upstream request queue is saturated."""


class UpstreamUnavailableError(RuntimeError):
    """Raised when the upstream PiDog HTTP server cannot be reached."""


class UpstreamTimeoutError(RuntimeError):
    """Raised when an upstream PiDog HTTP call times out."""


@dataclass(slots=True)
class ProxyResult:
    status_code: int
    headers: dict[str, str]
    content: bytes


@dataclass(slots=True)
class _ProxyJob:
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


class PidogClient:
    """Bounded-concurrency async HTTP client for the upstream PiDog server.

    Concurrency is bounded by ``max_concurrency``; excess requests are queued
    up to ``max_queue_size``.  This keeps a slow/laggy Pi from exhausting the
    aiohttp connection pool when the gateway is bombarded with REST traffic.
    """

    def __init__(
        self,
        *,
        base_url_fn: Any,  # callable returning current base URL (may change at runtime)
        max_concurrency: int = 2,
        max_queue_size: int = 64,
        request_timeout: float = 30.0,
    ) -> None:
        self._base_url_fn = base_url_fn
        self._max_concurrency = max(1, max_concurrency)
        self._request_timeout = request_timeout
        self._queue: asyncio.Queue[_ProxyJob] = asyncio.Queue(maxsize=max_queue_size)
        self._start_lock = asyncio.Lock()
        self._workers: list[asyncio.Task[None]] = []
        self._client: httpx.AsyncClient | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        async with self._start_lock:
            if self._client is not None:
                return
            self._client = httpx.AsyncClient(timeout=self._request_timeout)
            self._workers = [
                asyncio.create_task(
                    self._worker(index), name=f"pidog-client-worker-{index}"
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
                job.future.set_exception(UpstreamUnavailableError("Client stopping"))
            self._queue.task_done()

        if client is not None:
            await client.aclose()

    @property
    def base_url(self) -> str:
        url = self._base_url_fn()
        return url.rstrip("/") if url else ""

    def is_configured(self) -> bool:
        return bool(self.base_url)

    # ── Generic request (used by camera proxy fallbacks) ──────────────

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
            _ProxyJob(
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

    async def _perform(self, job: _ProxyJob) -> ProxyResult:
        client = self._client
        if client is None:
            raise UpstreamUnavailableError("Client not started")

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

    # ── Semantic helpers ──────────────────────────────────────────────

    async def health(self) -> bool:
        """Return True iff upstream ``GET /health`` returns 200 within 2 s."""
        if not self.is_configured():
            return False
        try:
            await self.start()
            assert self._client is not None
            r = await self._client.get(f"{self.base_url}/health", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False

    async def execute_action(
        self, robot_model: str, robot_id: str, action_name: str
    ) -> dict[str, Any]:
        """POST an action to the upstream and return its JSON response.

        The upstream returns a JSON envelope shaped roughly:
        ``{"status": "ok", "action_id": "...", "code": 0, "message": "..."}``.
        We always coerce the result into that shape so the SDK's
        :class:`ActionExecuteResponse` can wrap it without re-validating
        upstream variants.
        """
        if not self.is_configured():
            raise UpstreamUnavailableError("Pidog IP not configured")
        url = f"{self.base_url}/api/{robot_model}/{robot_id}/action/{action_name}"
        result = await self.submit("POST", url)
        return self._coerce_action_envelope(result, action_name)

    async def emergency_stop(self) -> dict[str, Any]:
        """POST ``/stop`` on the upstream; emergency-stop the dog."""
        if not self.is_configured():
            raise UpstreamUnavailableError("Pidog IP not configured")
        url = f"{self.base_url}/stop"
        result = await self.submit("POST", url)
        return self._coerce_action_envelope(result, "stop")

    async def fetch_state(self, robot_model: str, robot_id: str) -> dict[str, Any]:
        """GET the full upstream debug state. Returns ``{}`` on any error."""
        if not self.is_configured():
            return {}
        url = f"{self.base_url}/api/{robot_model}/{robot_id}/debug/state"
        try:
            result = await self.submit("GET", url)
            if result.status_code == 200:
                import json

                return json.loads(result.content or b"{}")
        except Exception:
            logger.debug("Pidog state fetch failed", exc_info=True)
        return {}

    async def fetch_battery(
        self, robot_model: str, robot_id: str
    ) -> dict[str, Any]:
        """GET the upstream battery snapshot. Returns ``{}`` on any error."""
        if not self.is_configured():
            return {}
        url = f"{self.base_url}/api/{robot_model}/{robot_id}/battery"
        try:
            result = await self.submit("GET", url)
            if result.status_code == 200:
                import json

                return json.loads(result.content or b"{}")
        except Exception:
            logger.debug("Pidog battery fetch failed", exc_info=True)
        return {}

    def snapshot_url(self, robot_id: str, feed: str) -> str:
        """Build the upstream snapshot URL for a given feed."""
        if not self.is_configured():
            return ""
        return f"{self.base_url}/api/camera/{robot_id}/{feed}/snapshot"

    # ── Internals ─────────────────────────────────────────────────────

    @staticmethod
    def _coerce_action_envelope(
        result: ProxyResult, action_name: str
    ) -> dict[str, Any]:
        import json

        body: dict[str, Any] = {}
        if result.content:
            try:
                parsed = json.loads(result.content)
                if isinstance(parsed, dict):
                    body = parsed
            except Exception:
                body = {"raw": result.content[:256].decode("utf-8", errors="replace")}

        if result.status_code >= 400:
            return {
                "status": "error",
                "action_id": body.get("action_id"),
                "code": int(body.get("code", result.status_code)),
                "message": body.get("message", f"upstream {result.status_code}"),
            }

        return {
            "status": body.get("status", "ok"),
            "action_id": body.get("action_id"),
            "code": int(body.get("code", 0)),
            "message": body.get("message", ""),
        }
