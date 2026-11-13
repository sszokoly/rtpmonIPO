"""Focused tests for captured RTP events and connection lifecycle."""

import argparse
import asyncio
import contextlib
import io
import os
import re
import struct
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import WSMsgType

from rtpmonIPO import cli
from rtpmonIPO.client import IpoClient
from rtpmonIPO.errors import IpoConnectionError
from rtpmonIPO.protocol import parse_rtp_end
from rtpmonIPO.startup import (
    EVENT_FILTER,
    FILTER_QUERY,
    INITIALIZE,
    KEEPALIVE,
    POST_FILTER_COMMANDS,
)

# Text and header reconstructed from the captured 0x0233 RTP-end frame.
CAPTURED_TEXT = (
    "!SESS 170f: RTP(END): 10.92.32.3/46750 10.234.255.5/37152 "
    "CODEC=Ulaw64K(5) PKTSZ=160 RFC2833=on AGE=13356 SENT=660 "
    "RECV=75 RTdelay=0 jitter=0 loss=0 remotejitter=0 remoteloss=0 "
)
CAPTURED_PAYLOAD = (
    struct.pack("!IIII", 0x1C2, 2, 0x233, 0x309127FF)
    + bytes(17)
    + CAPTURED_TEXT.encode("utf-16-be")
)


class ProtocolTests(unittest.TestCase):
    def test_credentials_load_dotenv_from_current_directory(self) -> None:
        try:
            import dotenv  # noqa: F401
        except ImportError:
            self.skipTest("python-dotenv is optional")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(
                "IPO_USER=file-user\nIPO_PASSWORD=file-password\n",
                encoding="utf-8",
            )
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch.dict(os.environ, {}, clear=True):
                    args = argparse.Namespace(ipo_user=None, ipo_passwd=None)
                    self.assertEqual(
                        cli._credentials(args), ("file-user", "file-password")
                    )
            finally:
                os.chdir(previous)

    def test_captured_rtp_end(self) -> None:
        summary = parse_rtp_end(
            CAPTURED_PAYLOAD, datetime(2026, 1, 1, tzinfo=UTC)
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(
            (summary.sess, summary.sent, summary.recv), ("170f", 660, 75)
        )
        self.assertEqual(summary.remote_ip, "10.234.255.5")

    def test_address_file_and_invalid_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "addresses.txt"
            path.write_text(" 192.0.2.1 \n\n2001:db8::1\n", encoding="utf-8")
            args = cli.create_parser().parse_args(
                ["-f", str(path), "192.0.2.2"]
            )
            self.assertEqual(
                args.file,
                [cli._IpoTarget("192.0.2.1"), cli._IpoTarget("2001:db8::1")],
            )
            self.assertEqual(args.ipo_addresses, ["192.0.2.2"])
            path.write_text("192.0.2.1\ninvalid\n", encoding="utf-8")
            with self.assertRaisesRegex(argparse.ArgumentTypeError, r":2:"):
                cli._address_file(str(path))

    def test_csv_credentials_and_invalid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "addresses.csv"
            path.write_text(
                '\n# IPO inventory\nip,username,password,comment\n'
                '#192.0.2.99,ignored,ignored,#Disabled\n'
                '  #192.0.2.98,ignored,ignored,#Disabled\n'
                '192.0.2.1,site-user," secret,pw ",#Location1\n'
                '2001:db8::1,,site-password,"Location, two"\n'
                '192.0.2.2,user\n',
                encoding="utf-8",
            )
            self.assertEqual(
                cli._address_file(str(path)),
                [
                    cli._IpoTarget("192.0.2.1", "site-user", " secret,pw "),
                    cli._IpoTarget("2001:db8::1", None, "site-password"),
                    cli._IpoTarget("192.0.2.2", "user"),
                ],
            )
            for row in (
                "192.0.2.1,user,secret,comment,extra\n",
                "invalid,user,secret\n",
                '192.0.2.1,user,"secret\n',
            ):
                with self.subTest(row=row):
                    path.write_text(row, encoding="utf-8")
                    with self.assertRaises(argparse.ArgumentTypeError) as raised:
                        cli._address_file(str(path))
                    self.assertIn(f"{path}:", str(raised.exception))
                    self.assertNotIn("secret", str(raised.exception))

    def test_main_accepts_file_only_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "addresses.csv"
            path.write_text("192.0.2.1,user,password\n", encoding="utf-8")
            with patch.object(cli, "run", new_callable=AsyncMock) as run:
                run.return_value = 0
                self.assertEqual(cli.main(["--file", str(path)]), 0)
            self.assertEqual(
                run.call_args.args[0].file,
                [cli._IpoTarget("192.0.2.1", "user", "password")],
            )


class StartupTests(unittest.IsolatedAsyncioTestCase):
    def test_startup_matches_h323_only_capture(self) -> None:
        path = Path(__file__).resolve().parents[1] / "data/ipo_h323evt_only.txt"
        if not path.exists():
            self.skipTest("local H323Evt-only capture is unavailable")
        capture = path.read_text(encoding="utf-8")
        frame = capture.split("Frame 27: Packet", 1)[1].split(
            "Frame 28: Packet", 1
        )[0]
        rows = re.findall(
            r"^[0-9a-f]{4}  ((?:[0-9a-f]{2} ){1,16})", frame, re.MULTILINE
        )
        self.assertEqual(EVENT_FILTER, bytes.fromhex("".join(rows)))
        self.assertEqual(len(EVENT_FILTER), 583)
        frame = capture.split("Frame 29: Packet", 1)[1].split(
            "Frame 30: Packet", 1
        )[0]
        commands = re.findall(r"^    Data: ([0-9a-f]+)$", frame, re.MULTILINE)
        self.assertEqual(
            POST_FILTER_COMMANDS, tuple(bytes.fromhex(c) for c in commands)
        )

    async def test_keepalive_sends_and_stops_on_close(self) -> None:
        class Socket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[bytes] = []
                self.sent_event = asyncio.Event()

            async def send_bytes(self, value: bytes) -> None:
                self.sent.append(value)
                self.sent_event.set()

            async def close(self) -> None:
                self.closed = True

        client = IpoClient(
            "http://192.0.2.1", username="u", password="p",
            keepalive_interval=0.01,
        )
        socket = Socket()

        async def open_socket() -> None:
            client._websocket = socket

        async def start_monitor() -> None:
            pass

        with (
            patch.object(client, "_open_websocket", open_socket),
            patch.object(client, "_start_monitor", start_monitor),
        ):
            await client.connect()
            task = client._keepalive_task
            await asyncio.wait_for(socket.sent_event.wait(), timeout=1)
            await client.close()
            self.assertEqual(socket.sent, [KEEPALIVE])
            self.assertIsNotNone(task)
            assert task is not None
            self.assertTrue(task.done())
            self.assertTrue(socket.closed)

    async def test_startup_sequence_and_pending_event(self) -> None:
        def event(event_id: int) -> bytes:
            return struct.pack("!IIII", 1, 2, event_id, 0)

        class Socket:
            def __init__(self) -> None:
                self.sent: list[bytes] = []
                self.messages = iter(
                    [CAPTURED_PAYLOAD, event(0xC7), event(0x1FB)]
                )

            async def send_bytes(self, value: bytes) -> None:
                self.sent.append(value)

            async def receive(self) -> SimpleNamespace:
                if len(self.sent) > 1:
                    self_expected = [
                        INITIALIZE, FILTER_QUERY, EVENT_FILTER,
                        *POST_FILTER_COMMANDS,
                    ]
                    if self.sent != self_expected:
                        raise AssertionError("waiting before requests were sent")
                return SimpleNamespace(
                    type=WSMsgType.BINARY, data=next(self.messages)
                )

        client = IpoClient("http://192.0.2.1", username="u", password="p")
        socket = Socket()
        client._websocket = socket
        await client._start_monitor()
        self.assertEqual(
            socket.sent,
            [INITIALIZE, FILTER_QUERY, EVENT_FILTER, *POST_FILTER_COMMANDS],
        )
        self.assertEqual(len(client._pending_messages), 1)
        self.assertEqual(client._pending_messages[0][0], CAPTURED_PAYLOAD)


class CliTests(unittest.IsolatedAsyncioTestCase):
    def test_age_is_in_standard_output_and_not_duplicated_in_verbose(self) -> None:
        summary = parse_rtp_end(CAPTURED_PAYLOAD, datetime.now(UTC))
        assert summary is not None
        standard_header = cli._summary_header(False)
        standard_row = cli._format_summary(summary, False)
        self.assertEqual(
            standard_header.split(),
            ["Date/Time", "LocalIP", "LPort", "RemoteIP", "RPort",
             "Codec", "Sent", "Recv", "Age"],
        )
        age_start = standard_header.index("Age")
        self.assertEqual(standard_header[age_start:], "Age     ")
        self.assertEqual(standard_row[age_start:], "13356   ")
        self.assertTrue(cli._summary_header(True).startswith(standard_header + "  Delay"))
        self.assertTrue(cli._format_summary(summary, True).startswith(standard_row + "  "))
        self.assertEqual(cli._summary_header(True).split().count("Age"), 1)

    async def test_idle_startup_heading_follows_last_connection_update(self) -> None:
        waiting = asyncio.Event()
        streams = 0

        class Client:
            def __init__(self, url, **kwargs):
                self.websocket_url = url

            async def connect(self):
                await asyncio.sleep(0)

            async def close(self):
                pass

            async def rtp_summaries(self):
                nonlocal streams
                streams += 1
                if streams == 2:
                    waiting.set()
                await asyncio.Event().wait()
                yield  # Make this an idle async generator.

        args = cli.create_parser().parse_args([
            "--ipo-user", "user", "--ipo-passwd", "password",
            "192.0.2.1", "192.0.2.2",
        ])
        output = io.StringIO()
        with patch.object(cli, "IpoClient", Client), contextlib.redirect_stdout(output):
            task = asyncio.create_task(cli.run(args))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                lines = output.getvalue().splitlines()
                self.assertEqual(lines[-1], cli._summary_header(False))
                self.assertTrue(lines[-2].startswith("Connected to "))
                self.assertEqual(output.getvalue().count("Connected to "), 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    def test_rotate_argument(self) -> None:
        parser = cli.create_parser()
        self.assertEqual(parser.parse_args([]).rotate, 7)
        for value in ("0", "14"):
            self.assertEqual(
                parser.parse_args(["--rotate", value]).rotate, int(value)
            )
        for value in ("-1", "1.5", "invalid"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["--rotate", value])

    def test_keepalive_interval_argument(self) -> None:
        parser = cli.create_parser()
        self.assertEqual(
            parser.parse_args([]).keepalive_interval, cli.KEEPALIVE_INTERVAL
        )
        self.assertEqual(
            parser.parse_args(["--keepalive-interval", "25"]).keepalive_interval,
            25.0,
        )
        for value in ("0", "-1", "nan", "inf", "invalid"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["--keepalive-interval", value])

    async def test_per_target_credentials_and_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "addresses.csv"
            path.write_text(
                "#192.0.2.99,ignored,ignored,#Disabled\n"
                "192.0.2.1,file-user,file-password,#Location1\n"
                "192.0.2.2,,file-password,#LocationA\n"
                "192.0.2.3,file-user,,#LocationB\n"
                "192.0.2.4,,,#LocationC\n",
                encoding="utf-8",
            )
            for source in ("cli", "environment", "file-only"):
                with self.subTest(source=source):
                    argv = [
                        "--file", str(path), "--keepalive-interval", "25",
                        "--protocol", "https", "--port", "8443",
                        "--insecure", "--connection-timeout", "3",
                    ]
                    if source == "cli":
                        argv += [
                            "--ipo-user", "cli-user",
                            "--ipo-passwd", "cli-password",
                        ]
                    if source != "file-only":
                        argv.append("192.0.2.10")
                    args = cli.create_parser().parse_args(argv)
                    if source == "file-only":
                        args.file = args.file[:1]
                    clients = []

                    def make_client(url, **kwargs):
                        client = MagicMock(websocket_url=url)
                        client.connect = AsyncMock(
                            side_effect=IpoConnectionError(url, "offline")
                        )
                        client.close = AsyncMock()
                        clients.append(client)
                        return client

                    environment = (
                        {} if source == "file-only" else
                        {"IPO_USER": "env-user", "IPO_PASSWORD": "env-password"}
                    )
                    target_count = len(args.file) + len(args.ipo_addresses)
                    barrier = asyncio.Barrier(target_count)
                    retried = asyncio.Event()
                    waits = 0

                    async def retry_wait(seconds):
                        nonlocal waits
                        waits += 1
                        if waits <= target_count:
                            await barrier.wait()
                        else:
                            if waits == target_count * 2:
                                retried.set()
                            await asyncio.Event().wait()

                    with (
                        patch.dict(os.environ, environment, clear=True),
                        patch.object(cli, "load_dotenv"),
                        patch.object(cli, "IpoClient", side_effect=make_client)
                        as factory,
                        patch.object(cli, "_retry_wait", retry_wait),
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        task = asyncio.create_task(cli.run(args))
                        try:
                            await asyncio.wait_for(retried.wait(), 1)
                            self.assertFalse(task.done())
                        finally:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                    actual = [
                        (call.kwargs["username"], call.kwargs["password"])
                        for call in factory.call_args_list
                    ]
                    if source == "file-only":
                        expected = [("file-user", "file-password")]
                    else:
                        user, password = (
                            ("cli-user", "cli-password") if source == "cli"
                            else ("env-user", "env-password")
                        )
                        expected = [
                            (user, password),
                            ("file-user", "file-password"),
                            (user, "file-password"),
                            ("file-user", password),
                            (user, password),
                        ]
                    self.assertEqual(actual[:target_count], expected)
                    for call in factory.call_args_list[target_count:]:
                        original = next(
                            first for first in factory.call_args_list[:target_count]
                            if first.args == call.args
                        )
                        self.assertEqual(call, original)
                    for call in factory.call_args_list:
                        self.assertEqual(call.kwargs["keepalive_interval"], 25)
                        self.assertEqual(call.kwargs["startup_timeout"], 3)
                        self.assertTrue(call.kwargs["insecure"])
                        self.assertTrue(call.args[0].startswith("https://"))
                        self.assertTrue(call.args[0].endswith(":8443"))
                    for client in clients:
                        client.close.assert_awaited_once()

    async def test_partial_connection_and_independent_stream_failure(
        self,
    ) -> None:
        class Client:
            def __init__(self, url: str, **kwargs: object) -> None:
                self.websocket_url = url

            async def connect(self) -> None:
                if "192.0.2.3" in self.websocket_url:
                    raise IpoConnectionError(self.websocket_url, "timeout")

            async def close(self) -> None:
                pass

            async def rtp_summaries(self):
                if "192.0.2.1" in self.websocket_url:
                    raise ConnectionError("lost")
                await asyncio.sleep(0)
                yield parse_rtp_end(CAPTURED_PAYLOAD, datetime.now(UTC))

        args = argparse.Namespace(
            protocol="http",
            port=80,
            ipo_user="u",
            ipo_passwd="p",
            ipo_addresses=["192.0.2.1", "192.0.2.2", "192.0.2.3"],
            insecure=False,
            connection_timeout=1.0,
            verbose=False,
            bad_only=False,
        )
        output, errors = io.StringIO(), io.StringIO()
        waiting = asyncio.Event()
        waits = 0

        async def retry_wait(seconds):
            nonlocal waits
            waits += 1
            if waits == 3:
                waiting.set()
            await asyncio.Event().wait()

        with (
            patch.object(cli, "IpoClient", Client),
            patch.object(cli, "_retry_wait", retry_wait),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            task = asyncio.create_task(cli.run(args))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                self.assertFalse(task.done())
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertIn("Ulaw64K", output.getvalue())
        self.assertIn("192.0.2.3", errors.getvalue())
        self.assertIn("192.0.2.1", errors.getvalue())

    async def test_stalled_startup_does_not_delay_healthy_target(self) -> None:
        received = asyncio.Event()
        closed = []

        class Client:
            def __init__(self, url, **kwargs):
                self.websocket_url = url

            async def connect(self):
                if "192.0.2.1" in self.websocket_url:
                    await asyncio.Event().wait()

            async def close(self):
                closed.append(self.websocket_url)

            async def rtp_summaries(self):
                yield parse_rtp_end(CAPTURED_PAYLOAD, datetime.now(UTC))
                received.set()
                await asyncio.Event().wait()

        args = cli.create_parser().parse_args([
            "--ipo-user", "user", "--ipo-passwd", "password",
            "192.0.2.1", "192.0.2.2",
        ])
        output = io.StringIO()
        with patch.object(cli, "IpoClient", Client), contextlib.redirect_stdout(output):
            task = asyncio.create_task(cli.run(args))
            try:
                await asyncio.wait_for(received.wait(), 1)
                self.assertIn("Ulaw64K", output.getvalue())
                self.assertFalse(task.done())
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(len(closed), 2)


if __name__ == "__main__":
    unittest.main()
