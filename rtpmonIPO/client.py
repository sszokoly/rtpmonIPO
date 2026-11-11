"""Connection configuration for one SysMonitor endpoint."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from urllib.parse import SplitResult, urlsplit, urlunsplit

from aiohttp import (
    ClientConnectorCertificateError,
    ClientError,
    ClientSession,
    ClientTimeout,
    ClientWebSocketResponse,
    DigestAuthMiddleware,
    WSMsgType,
    WSServerHandshakeError,
)

from .errors import IpoConnectionError
from .models import RtpSummary
from .protocol import is_rtp_end_candidate, parse_rtp_end
from .startup import (
    EVENT_FILTER,
    FILTER_COMPLETE_EVENT,
    FILTER_QUERY,
    INITIALIZE,
    INITIALIZE_COMPLETE_EVENT,
    KEEPALIVE,
    POST_FILTER_COMMANDS,
)

CLIENT_LOGGER = logging.getLogger(__name__)
MAX_PENDING_SUMMARIES = 128
KEEPALIVE_INTERVAL = 10.0


class IpoClient:
    """Own one authenticated connection to an IP Office SysMonitor endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        insecure: bool = False,
        startup_timeout: float = 10.0,
        keepalive_interval: float = KEEPALIVE_INTERVAL,
    ) -> None:
        if not username or not password:
            raise ValueError("username and password are required")
        if not isinstance(insecure, bool):
            raise TypeError("insecure must be a boolean")
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")
        if not math.isfinite(keepalive_interval) or keepalive_interval <= 0:
            raise ValueError("keepalive_interval must be finite and positive")

        self._endpoint: str = _normalize_endpoint(base_url)
        self._username: str = username
        self._password: str = password
        self._insecure: bool = insecure
        self._startup_timeout: float = startup_timeout
        self._keepalive_interval: float = keepalive_interval
        self._session: ClientSession | None = None
        self._websocket: ClientWebSocketResponse | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._pending_messages: deque[tuple[bytes, datetime]] = deque()

    @property
    def websocket_url(self) -> str:
        """Normalized SysMonitor WebSocket endpoint without credentials."""
        return self._endpoint

    async def connect(self) -> None:
        """Open and initialize the authenticated SysMonitor connection."""
        try:
            await self._open_websocket()
            await self._start_monitor()
            self._keepalive_task = asyncio.create_task(self._send_keepalives())
        except (TimeoutError, ClientError, ConnectionError, OSError) as error:
            await self._close_transport()
            raise IpoConnectionError(
                self._endpoint, _connection_failure_reason(error)
            ) from error
        except BaseException:
            await self._close_transport()
            raise

    async def close(self) -> None:
        """Close the WebSocket connection and its HTTP session."""
        await self._close_transport()

    async def rtp_summaries(self) -> AsyncGenerator[RtpSummary, None]:
        """Yield decoded RTP-end summaries from the WebSocket."""
        if self._websocket is None:
            raise RuntimeError("connection is not open")

        try:
            while self._pending_messages:
                payload, received_at = self._pending_messages.popleft()
                summary = self._parse_summary(payload, received_at)
                if summary is not None:
                    yield summary

            async for message in self._websocket:
                if message.type is WSMsgType.BINARY:
                    summary = self._parse_summary(
                        message.data, datetime.now(UTC)
                    )
                    if summary is not None:
                        yield summary
                elif message.type is WSMsgType.ERROR:
                    error = self._websocket.exception()
                    raise RuntimeError("WebSocket connection failed") from error
        finally:
            await self._stop_keepalives()

    async def _send_keepalives(self) -> None:
        """Send the periodic SysMonitor request observed in Monitor captures."""
        websocket = self._websocket
        if websocket is None:
            return
        try:
            while not websocket.closed:
                await asyncio.sleep(self._keepalive_interval)
                if websocket.closed:
                    break
                await websocket.send_bytes(KEEPALIVE)
                CLIENT_LOGGER.debug(
                    "Sent SysMonitor keepalive to %s", self._endpoint
                )
        except (ClientError, ConnectionError, OSError, RuntimeError):
            CLIENT_LOGGER.warning(
                "SysMonitor keepalive failed for %s",
                self._endpoint,
                exc_info=True,
            )
            await websocket.close()

    async def _stop_keepalives(self) -> None:
        """Cancel and await the connection's keepalive task."""
        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _parse_summary(
        self, payload: bytes, received_at: datetime
    ) -> RtpSummary | None:
        summary = parse_rtp_end(payload, received_at)
        if summary is None and is_rtp_end_candidate(payload):
            CLIENT_LOGGER.debug(
                "Could not decode RTP-end event from %s", self._endpoint
            )
        return summary

    async def _start_monitor(self) -> None:
        """Perform the captured Monitor startup exchange required for events."""
        if self._websocket is None:
            raise RuntimeError("connection is not open")

        CLIENT_LOGGER.debug("Sending SysMonitor initialization")
        await self._websocket.send_bytes(INITIALIZE)
        await self._wait_for_event(INITIALIZE_COMPLETE_EVENT)

        CLIENT_LOGGER.debug("Installing SysMonitor event filters")
        await self._websocket.send_bytes(FILTER_QUERY)
        await self._websocket.send_bytes(EVENT_FILTER)
        for command in POST_FILTER_COMMANDS:
            await self._websocket.send_bytes(command)
        await self._wait_for_event(FILTER_COMPLETE_EVENT)

    async def _wait_for_event(self, event_id: int) -> None:
        """Read startup messages through the requested IPO event identifier."""
        if self._websocket is None:
            raise RuntimeError("connection is not open")

        try:
            async with asyncio.timeout(self._startup_timeout):
                while True:
                    message = await self._websocket.receive()
                    if message.type is WSMsgType.BINARY:
                        payload = message.data
                        if is_rtp_end_candidate(payload):
                            if (
                                len(self._pending_messages)
                                >= MAX_PENDING_SUMMARIES
                            ):
                                self._pending_messages.popleft()
                                CLIENT_LOGGER.warning(
                                    "Startup RTP buffer full for %s; "
                                    "discarding oldest event",
                                    self._endpoint,
                                )
                            self._pending_messages.append(
                                (payload, datetime.now(UTC))
                            )
                        if _event_id(payload) == event_id:
                            return
                    elif message.type is WSMsgType.ERROR:
                        error = self._websocket.exception()
                        raise RuntimeError(
                            "WebSocket connection failed"
                        ) from error
                    elif message.type in {
                        WSMsgType.CLOSE,
                        WSMsgType.CLOSED,
                        WSMsgType.CLOSING,
                    }:
                        raise ConnectionError(
                            "WebSocket closed during SysMonitor startup"
                        )
        except TimeoutError as error:
            message = "timed out waiting for SysMonitor startup event "
            raise TimeoutError(f"{message}0x{event_id:04x}") from error

    async def _open_websocket(self) -> None:
        """Open the transport before SysMonitor initialization."""
        if self._session is not None:
            raise RuntimeError("connection is already open")

        middleware = DigestAuthMiddleware(self._username, self._password)
        self._session = ClientSession(
            middlewares=(middleware,),
            timeout=ClientTimeout(total=self._startup_timeout),
        )
        try:
            self._websocket = await self._session.ws_connect(
                self._endpoint,
                protocols=("sysmonitor",),
                autoping=True,
                compress=0,
                ssl=not (
                    self._insecure and self._endpoint.startswith("wss://")
                ),
            )
        except BaseException:
            await self._close_transport()
            raise

    async def _close_transport(self) -> None:
        """Release any partially acquired transport resources."""
        await self._stop_keepalives()
        if self._websocket is not None:
            await self._websocket.close()
            self._websocket = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._pending_messages.clear()


def _event_id(payload: bytes) -> int | None:
    """Return a binary SysMonitor event identifier, when present."""
    if len(payload) < 12:
        return None
    return int.from_bytes(payload[8:12], "big")


def _connection_failure_reason(error: BaseException) -> str:
    """Return a safe, useful description of a transport connection failure."""
    if isinstance(error, TimeoutError):
        return "connection timed out"
    if isinstance(error, ClientConnectorCertificateError):
        return "TLS certificate verification failed"
    if isinstance(error, WSServerHandshakeError):
        return f"WebSocket handshake failed (HTTP {error.status})"
    return str(error) or error.__class__.__name__


def _normalize_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute http or https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "base_url must not include credentials, query, or fragment"
        )
    if parsed.path not in {"", "/"}:
        raise ValueError("base_url must not include a path")

    scheme = "ws" if parsed.scheme == "http" else "wss"
    return urlunsplit(
        SplitResult(scheme, parsed.netloc, "/sysmonitor/", "", "")
    )
