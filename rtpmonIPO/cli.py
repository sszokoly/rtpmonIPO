"""Command-line connection setup for IP Office SysMonitor endpoints."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import math
import os
import sys
from collections.abc import Sequence
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
from .client import IpoClient
from .db import SummaryDatabase
from .errors import IpoConnectionError
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


def _address_file(filename: str) -> list[str]:
    """Read and validate IPO addresses from a UTF-8 text file."""
    addresses: list[str] = []
    try:
        with Path(filename).open(encoding="utf-8") as file:
            for number, line in enumerate(file, start=1):
                value = line.strip()
                if not value:
                    continue
                try:
                    addresses.append(_ipo_address(value))
                except argparse.ArgumentTypeError as error:
                    raise argparse.ArgumentTypeError(
                        f"{filename}:{number}: {error}: {value!r}"
                    ) from error
    except (OSError, UnicodeError) as error:
        raise argparse.ArgumentTypeError(
            f"cannot read {filename!r}: {error}"
        ) from error
    return addresses


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
        "-f",
        "--file",
        type=_address_file,
        default=[],
        metavar="FILE",
        help="read IPO IP addresses from a file, one per line",
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
    if not username or not password:
        raise ValueError("--ipo-user and --ipo-passwd must not be empty")

    clients: list[IpoClient] = [
        IpoClient(
            _base_url(arguments.protocol, address, arguments.port),
            username=username,
            password=password,
            insecure=getattr(arguments, "insecure", False),
            startup_timeout=getattr(
                arguments, "connection_timeout", CONNECTION_TIMEOUT
            ),
        )
        for address in arguments.ipo_addresses
    ]
    database = (
        SummaryDatabase(arguments.db)
        if getattr(arguments, "db", None)
        else None
    )
    try:
        if database is not None:
            await database.open()
        for client in clients:
            LOGGER.debug(
                "Opening WebSocket connection to %s", client.websocket_url
            )
        results: list[object] = list(
            await asyncio.gather(
                *(client.connect() for client in clients),
                return_exceptions=True,
            )
        )
        connected_clients: list[IpoClient] = []
        failures: list[IpoConnectionError] = []
        for client, result in zip(clients, results, strict=True):
            if result is None:
                connected_clients.append(client)
            elif isinstance(result, IpoConnectionError):
                failures.append(result)
            elif isinstance(result, Exception):
                failures.append(
                    IpoConnectionError(
                        client.websocket_url,
                        str(result) or result.__class__.__name__,
                    )
                )
            elif isinstance(result, BaseException):
                raise result
            else:
                raise RuntimeError(
                    "connection task returned an unexpected result"
                )

        if failures:
            for failure in failures:
                LOGGER.debug("IPO connection failure: %s", failure)
                print(f"error: {failure}", file=sys.stderr)
        if not connected_clients:
            return 1

        for client in connected_clients:
            LOGGER.debug(
                "WebSocket connection established to %s", client.websocket_url
            )
            print(f"Connected to {client.websocket_url}", flush=True)

        verbose = getattr(arguments, "verbose", False)
        bad_only = getattr(arguments, "bad_only", False)
        print(_summary_header(verbose), flush=True)

        async with asyncio.TaskGroup() as group:
            for client in connected_clients:
                group.create_task(
                    _print_summaries(client, verbose, bad_only, database)
                )
        return 0
    finally:
        LOGGER.debug("Closing %d WebSocket connection(s)", len(clients))
        await asyncio.gather(
            *(client.close() for client in clients), return_exceptions=True
        )
        if database is not None:
            await database.close()


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
) -> None:
    """Print each decoded RTP summary received from one IPO."""
    iterator = client.rtp_summaries()
    while True:
        try:
            summary = await anext(iterator)
        except StopAsyncIteration:
            print(f"Disconnected from {client.websocket_url}", file=sys.stderr)
            return
        except Exception as error:
            LOGGER.debug(
                "Stream failure for %s", client.websocket_url, exc_info=True
            )
            print(
                f"error: stream from {client.websocket_url} failed: {error}",
                file=sys.stderr,
            )
            return

        try:
            LOGGER.debug("Received RTP summary from %s", client.websocket_url)
            if bad_only and not _is_bad_summary(summary):
                continue
            if database is not None:
                await database.write(summary)
            print(_format_summary(summary, verbose), flush=True)
        except Exception:
            await iterator.aclose()
            raise


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
        import urllib3
    except ImportError:
        return
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and run the asynchronous connection manager."""
    parser = create_parser()
    arguments = parser.parse_args(argv)
    arguments.ipo_addresses.extend(arguments.file)
    if not arguments.ipo_addresses:
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
