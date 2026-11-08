"""Connection configuration for one SysMonitor endpoint."""

from __future__ import annotations

import asyncio
from urllib.parse import SplitResult, urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, DigestAuthMiddleware


class IpoClient:
    """Own one authenticated connection; live startup is device-gated."""

    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        insecure: bool = False,
        startup_timeout: float = 10.0,
    ) -> None:
        if not username or not password:
            raise ValueError("username and password are required")
        if not isinstance(insecure, bool):
            raise TypeError("insecure must be a boolean")
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")

        self._endpoint = _normalize_endpoint(base_url)
        self._username = username
        self._password = password
        self._insecure = insecure
        self._startup_timeout = startup_timeout
        self._session: ClientSession | None = None
        self._websocket = None

    @property
    def websocket_url(self) -> str:
        """Normalized SysMonitor WebSocket endpoint without credentials."""
        return self._endpoint

    async def _open_websocket(self) -> None:
        """Open the authenticated transport before the device-gated startup phase."""
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
                ssl=False if self._insecure and self._endpoint.startswith("wss://") else True,
            )
        except BaseException:
            await self._close_transport()
            raise

    async def _close_transport(self) -> None:
        """Release any partially acquired transport resources."""
        if self._websocket is not None:
            await self._websocket.close()
            self._websocket = None
        if self._session is not None:
            await self._session.close()
            self._session = None


def _normalize_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute http or https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url must not include credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("base_url must not include a path")

    scheme = "ws" if parsed.scheme == "http" else "wss"
    return urlunsplit(SplitResult(scheme, parsed.netloc, "/sysmonitor/", "", ""))
