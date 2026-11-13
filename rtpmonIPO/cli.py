"""Command-line connection setup for IP Office SysMonitor endpoints."""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib
import ipaddress
import logging
import math
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    from dotenv import find_dotenv, load_dotenv
except ImportError:

    def find_dotenv(*, usecwd: bool = False) -> str:
        """Provide a no-op fallback when python-dotenv is unavailable."""
        return ""

    def load_dotenv(dotenv_path: str = "") -> bool:
        """Provide a no-op fallback when python-dotenv is unavailable."""
        return False


from . import __version__
from .client import KEEPALIVE_INTERVAL, IpoClient
from .db import DEFAULT_ROTATE, SummaryDatabase
from .models import RtpSummary

IPO_PROTO = "http"
IPO_PORT = 80
IPO_USER = "Administrator"
IPO_PASSWORD = ""
CONNECTION_TIMEOUT = 5.0


LOGGER = logging.getLogger(__name__)


def _port(value: str) -> int:
    """Return a valid TCP port for ``argparse``."""
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _archive_count(value: str) -> int:
    """Return a non-negative archive retention count for argparse."""
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "rotate must be a non-negative integer"
        ) from error
    if count < 0:
        raise argparse.ArgumentTypeError("rotate must be a non-negative integer")
    return count


def _ipo_address(value: str) -> str:
    """Validate and normalize an IP Office IP address."""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "IPO address must be a valid IP address"
        ) from error


def _positive_seconds(value: str) -> float:
    """Return a finite, positive timeout duration."""
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return seconds


@dataclass(frozen=True)
class _IpoTarget:
    address: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _SessionSettings:
    """Resolved connection settings retained unchanged across retry attempts."""

    base_url: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    insecure: bool
    startup_timeout: float
    keepalive_interval: float

    def create_client(self) -> IpoClient:
        return IpoClient(
            self.base_url,
            username=self.username,
            password=self.password,
            insecure=self.insecure,
            startup_timeout=self.startup_timeout,
            keepalive_interval=self.keepalive_interval,
        )

    def safe_reason(self, error: Exception) -> str:
        """Redact credentials even when a peer includes them in an error."""
        secrets = sorted(
            {self.username, self.password} - {""}, key=len, reverse=True
        )
        reason = str(error) or error.__class__.__name__
        return re.sub(
            "|".join(re.escape(secret) for secret in secrets),
            "[redacted]",
            reason,
        )


def _address_file(filename: str) -> list[_IpoTarget]:
    """Read IPO addresses and optional per-IPO credentials from UTF-8 CSV."""
    targets: list[_IpoTarget] = []
    reader = None
    try:
        with Path(filename).open(encoding="utf-8", newline="") as file:
            reader = csv.reader(file, strict=True)
            first_row = True
            for row in reader:
                if not row or (len(row) == 1 and not row[0].strip()):
                    continue
                if row[0].lstrip().startswith("#"):
                    continue
                if first_row and row in (
                    ["ip", "username", "password"],
                    ["ip", "username", "password", "comment"],
                ):
                    first_row = False
                    continue
                first_row = False
                number = reader.line_num
                if not 1 <= len(row) <= 4:
                    raise argparse.ArgumentTypeError(
                        f"{filename}:{number}: expected "
                        "ip[,username[,password[,comment]]]"
                    )
                try:
                    address = _ipo_address(row[0].strip())
                except argparse.ArgumentTypeError as error:
                    raise argparse.ArgumentTypeError(
                        f"{filename}:{number}: {error}"
                    ) from error
                targets.append(
                    _IpoTarget(
                        address,
                        row[1] or None if len(row) > 1 else None,
                        row[2] or None if len(row) > 2 else None,
                    )
                )
    except csv.Error as error:
        number = reader.line_num if reader is not None else 0
        raise argparse.ArgumentTypeError(
            f"{filename}:{number}: invalid CSV: {error}"
        ) from error
    except (OSError, UnicodeError) as error:
        raise argparse.ArgumentTypeError(
            f"cannot read {filename!r}: {error}"
        ) from error
    return targets


def create_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Connect to one or more Avaya IP Office SysMonitor WebSockets."
        )
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable diagnostic logging on standard error",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include RTP delay, jitter, and loss metrics",
    )
    parser.add_argument(
        "--bad-only",
        action="store_true",
        help="print only RTP summaries classified as bad",
    )
    parser.add_argument(
        "--db", metavar="FILE", help="store printed summaries in a SQLite file"
    )
    parser.add_argument(
        "--rotate",
        type=_archive_count,
        default=DEFAULT_ROTATE,
        metavar="COUNT",
        help=(
            "number of daily database archives to retain, excluding the "
            f"current database (default: {DEFAULT_ROTATE}; 0 keeps none)"
        ),
    )
    parser.add_argument(
        "-f",
        "--file",
        type=_address_file,
        default=[],
        metavar="FILE",
        help=(
            "read IPOs from CSV: ip[,username[,password[,comment]]]; "
            "ignore # comment rows; file credentials override "
            "--ipo-user and --ipo-passwd"
        ),
    )
    parser.add_argument(
        "--protocol",
        choices=("http", "https"),
        default=IPO_PROTO,
        help=f"Web API protocol (default: {IPO_PROTO})",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate verification for HTTPS connections",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=IPO_PORT,
        help=f"Web API port (default: {IPO_PORT})",
    )
    parser.add_argument(
        "--connection-timeout",
        "--timeout",
        dest="connection_timeout",
        type=_positive_seconds,
        default=CONNECTION_TIMEOUT,
        help=(
            "seconds to wait for the WebSocket connection and SysMonitor "
            f"startup (default: {CONNECTION_TIMEOUT:g})"
        ),
    )
    parser.add_argument(
        "--keepalive-interval",
        type=_positive_seconds,
        default=KEEPALIVE_INTERVAL,
        metavar="SECONDS",
        help=(
            "seconds between SysMonitor keepalives "
            f"(default: {KEEPALIVE_INTERVAL:g})"
        ),
    )
    parser.add_argument(
        "--ipo-user",
        "--ipo_user",
        dest="ipo_user",
        default=None,
        help=f"Web API username (environment: IPO_USER; default: {IPO_USER})",
    )
    parser.add_argument(
        "--ipo-passwd",
        "--ipo_passwd",
        dest="ipo_passwd",
        default=None,
        help="Web API password (environment: IPO_PASSWORD)",
    )
    parser.add_argument(
        "ipo_addresses",
        metavar="IPO_IP_ADDRESS",
        nargs="*",
        type=_ipo_address,
        help="IP address of an IP Office system",
    )
    return parser


def _base_url(protocol: str, address: str, port: int) -> str:
    """Build an HTTP(S) base URL, correctly enclosing IPv6 literals."""
    host = f"[{address}]" if ":" in address else address
    return f"{protocol}://{host}:{port}"


async def run(arguments: argparse.Namespace) -> int:
    """Connect to every requested IPO and print its RTP summaries."""
    username, password = _credentials(arguments)
    targets = [
        _IpoTarget(address) for address in arguments.ipo_addresses
    ] + list(getattr(arguments, "file", []))
    credentials = [
        (target.username or username, target.password or password)
        for target in targets
    ]
    for target, (user, passwd) in zip(targets, credentials, strict=True):
        if not user or not passwd:
            raise ValueError(
                f"{target.address}: --ipo-user and --ipo-passwd must not be "
                "empty unless supplied in --file"
            )

    settings = [
        _SessionSettings(
            _base_url(arguments.protocol, target.address, arguments.port),
            username=user,
            password=passwd,
            keepalive_interval=getattr(
                arguments, "keepalive_interval", KEEPALIVE_INTERVAL
            ),
            insecure=getattr(arguments, "insecure", False),
            startup_timeout=getattr(
                arguments, "connection_timeout", CONNECTION_TIMEOUT
            ),
        )
        for target, (user, passwd) in zip(targets, credentials, strict=True)
    ]
    database = (
        SummaryDatabase(
            arguments.db, rotate=getattr(arguments, "rotate", DEFAULT_ROTATE)
        )
        if getattr(arguments, "db", None)
        else None
    )
    verbose = getattr(arguments, "verbose", False)
    bad_only = getattr(arguments, "bad_only", False)
    header_printed = False

    def connected(client: IpoClient) -> None:
        nonlocal header_printed
        print(f"Connected to {client.websocket_url}", flush=True)
        # No await between the check and print: supervisors cannot race here.
        if not header_printed:
            print(_summary_header(verbose), flush=True)
            header_printed = True

    try:
        if database is not None:
            await database.open()
        async with asyncio.TaskGroup() as group:
            for configuration in settings:
                group.create_task(
                    _monitor_target(
                        configuration, connected, verbose, bad_only, database
                    )
                )
        return 0
    finally:
        if database is not None:
            await database.close()


async def _retry_wait(seconds: float) -> None:
    """Wait without blocking other targets, and allow immediate cancellation."""
    await asyncio.sleep(seconds)


async def _monitor_target(
    settings: _SessionSettings,
    connected: Callable[[IpoClient], None],
    verbose: bool,
    bad_only: bool,
    database: SummaryDatabase | None,
) -> None:
    """Own one target's sessions and isolated bounded retry schedule."""
    delay = 1
    while True:
        # Constructor/configuration errors are fatal, not connection failures.
        client = settings.create_client()
        failure: Exception | None = None
        try:
            LOGGER.debug(
                "Opening WebSocket connection to %s", client.websocket_url
            )
            try:
                await client.connect()
            except Exception as error:
                failure = error
            else:
                delay = 1
                connected(client)
                # Receipt errors are returned; local processing errors escape.
                failure = await _print_summaries(
                    client, verbose, bad_only, database
                )
        finally:
            await client.close()

        if failure is None:
            message = f"Disconnected from {client.websocket_url}"
        else:
            message = (
                f"error: connection to {client.websocket_url} failed: "
                f"{settings.safe_reason(failure)}"
            )
        message += f"; retrying in {delay} seconds"
        LOGGER.debug("%s", message)
        print(message, file=sys.stderr, flush=True)
        await _retry_wait(delay)
        delay = min(delay * 2, 60)


def _credentials(arguments: argparse.Namespace) -> tuple[str, str]:
    """Resolve IPO credentials from CLI arguments, environment, then defaults."""
    load_dotenv(find_dotenv(usecwd=True))
    username = (
        arguments.ipo_user
        if arguments.ipo_user is not None
        else os.getenv("IPO_USER", IPO_USER)
    )
    password = (
        arguments.ipo_passwd
        if arguments.ipo_passwd is not None
        else os.getenv("IPO_PASSWORD", IPO_PASSWORD)
    )
    return username, password


async def _print_summaries(
    client: IpoClient,
    verbose: bool,
    bad_only: bool,
    database: SummaryDatabase | None = None,
) -> Exception | None:
    """Print each decoded RTP summary received from one IPO."""
    iterator = client.rtp_summaries()
    try:
        while True:
            try:
                summary = await anext(iterator)
            except StopAsyncIteration:
                return None
            except Exception as error:
                return error

            LOGGER.debug("Received RTP summary from %s", client.websocket_url)
            if bad_only and not _is_bad_summary(summary):
                continue
            if database is not None:
                await database.write(summary)
            print(_format_summary(summary, verbose), flush=True)
    finally:
        await iterator.aclose()


def _is_bad_summary(summary: RtpSummary) -> bool:
    """Return whether a summary meets the current bad-call criteria."""
    return summary.recv == 0


def _summary_header(verbose: bool) -> str:
    """Return the fixed-width heading for RTP summary output."""
    fields: list[tuple[str, int]] = [
        ("Date/Time", 19),
        ("LocalIP", 15),
        ("LPort", 5),
        ("RemoteIP", 15),
        ("RPort", 5),
        ("Codec", 7),
        ("Sent", 6),
        ("Recv", 6),
    ]
    if verbose:
        fields.extend(
            [
                ("Age", 8),
                ("Delay", 5),
                ("Jitter", 6),
                ("Loss", 6),
                ("RJitter", 7),
                ("RLoss", 6),
            ]
        )
    return "  ".join(f"{name:<{width}}" for name, width in fields)


def _format_summary(summary: RtpSummary, verbose: bool) -> str:
    """Format one RTP summary as a fixed-width, single-line record."""
    timestamp = (
        datetime.fromisoformat(summary.timestamp)
        .astimezone()
        .strftime("%Y-%m-%d@%H:%M:%S")
    )
    fields: list[tuple[object, int]] = [
        (timestamp, 19),
        (summary.local_ip, 15),
        (summary.local_rtp_port, 5),
        (summary.remote_ip, 15),
        (summary.remote_rtp_port, 5),
        (summary.codec, 7),
        (summary.sent, 6),
        (summary.recv, 6),
    ]
    if verbose:
        fields.extend(
            [
                (summary.age, 8),
                (summary.rtpdelay, 5),
                (summary.jitter, 6),
                (summary.loss, 6),
                (summary.remotejitter, 7),
                (summary.remoteloss, 6),
            ]
        )
    return "  ".join(f"{value:<{width}}" for value, width in fields)


def _disable_insecure_request_warnings() -> None:
    """Suppress urllib3 warnings when the user disables TLS checks."""
    try:
        urllib3 = importlib.import_module("urllib3")
    except ImportError:
        return
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and run the asynchronous connection manager."""
    parser = create_parser()
    arguments = parser.parse_args(argv)
    if not arguments.ipo_addresses and not arguments.file:
        parser.error(
            "provide at least one IPO IP address or a non-empty --file"
        )
    if arguments.insecure:
        _disable_insecure_request_warnings()
    if arguments.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    try:
        return asyncio.run(run(arguments))
    except ValueError as error:
        parser.error(str(error))
    except Exception as error:
        if arguments.debug:
            LOGGER.debug("rtpmonIPO failed", exc_info=error)
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
