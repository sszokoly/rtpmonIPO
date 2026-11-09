"""Exceptions exposed by rtpmonIPO."""

from __future__ import annotations

from collections.abc import Sequence


class IpoConnectionError(RuntimeError):
    """A connection or SysMonitor startup failure for one IPO endpoint."""

    def __init__(self, endpoint: str, reason: str) -> None:
        self.endpoint = endpoint
        self.reason = reason
        super().__init__(f"unable to connect to {endpoint}: {reason}")


class IpoConnectionFailures(RuntimeError):
    """Connection failures for one or more IPO endpoints."""

    def __init__(self, failures: Sequence[IpoConnectionError]) -> None:
        self.failures: tuple[IpoConnectionError, ...] = tuple(failures)
        super().__init__("\n".join(str(failure) for failure in self.failures))
