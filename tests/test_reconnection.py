"""Recovery scheduling, isolation, and resource lifecycle regressions."""

import asyncio
import contextlib
import io
import sqlite3
import struct
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import WSMsgType, web

from rtpmonIPO import cli
from rtpmonIPO.client import IpoClient
from rtpmonIPO.db import SummaryDatabase
from rtpmonIPO.errors import IpoConnectionError
from rtpmonIPO.protocol import parse_rtp_end
from rtpmonIPO.startup import (
    EVENT_FILTER,
    FILTER_COMPLETE_EVENT,
    FILTER_QUERY,
    INITIALIZE,
    INITIALIZE_COMPLETE_EVENT,
    KEEPALIVE,
    POST_FILTER_COMMANDS,
)
from tests.test_monitor import CAPTURED_PAYLOAD


def arguments(*extra):
    return cli.create_parser().parse_args([
        "--ipo-user", "monitor-user", "--ipo-passwd", "monitor-password",
        *extra,
    ])


async def cancel(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_connect_startup_and_receipt(self):
        for phase in ("connect", "startup", "receipt"):
            with self.subTest(phase=phase):
                reached = asyncio.Event()
                sibling_ready = asyncio.Event()
                clients, sessions, websockets, keepalives = [], [], [], []

                async def handler(request):
                    ws = web.WebSocketResponse(protocols=("sysmonitor",))
                    await ws.prepare(request)
                    message = await ws.receive()
                    if message.type != WSMsgType.BINARY:
                        return ws
                    await ws.send_bytes(struct.pack("!IIII", 1, 2, INITIALIZE_COMPLETE_EVENT, 0))
                    for _ in [FILTER_QUERY, EVENT_FILTER, *POST_FILTER_COMMANDS]:
                        message = await ws.receive()
                        if message.type != WSMsgType.BINARY:
                            return ws
                    await ws.send_bytes(struct.pack("!IIII", 2, 2, FILTER_COMPLETE_EVENT, 0))
                    async for _ in ws:
                        pass
                    return ws

                class Client(IpoClient):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        self.number = len(clients) + 1
                        self.closed = False
                        clients.append(self)

                    async def _open_websocket(self):
                        if self.number == 1 and phase == "connect":
                            reached.set()
                            await asyncio.Event().wait()
                        await super()._open_websocket()
                        sessions.append(self._session)
                        websockets.append(self._websocket)

                    async def _start_monitor(self):
                        if self.number == 1 and phase == "startup":
                            reached.set()
                            await asyncio.Event().wait()
                        await super()._start_monitor()

                    async def connect(self):
                        await super().connect()
                        keepalives.append(self._keepalive_task)
                        if self.number == 1:
                            reached.set()
                        else:
                            sibling_ready.set()

                    async def close(self):
                        await super().close()
                        self.closed = True

                database = MagicMock()
                database.open = AsyncMock()
                database.close = AsyncMock()
                app = web.Application()
                app.router.add_get("/sysmonitor/", handler)
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                try:
                    with (
                        patch.object(cli, "IpoClient", Client),
                        patch.object(cli, "SummaryDatabase", return_value=database),
                        patch.object(cli, "_retry_wait", new_callable=AsyncMock) as retry,
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        args = arguments(
                            "--db", "unused.db", "--port", str(runner.addresses[0][1]),
                            "127.0.0.1", "127.0.0.1",
                        )
                        task = asyncio.create_task(cli.run(args))
                        try:
                            await asyncio.wait_for(reached.wait(), 1)
                            await asyncio.wait_for(sibling_ready.wait(), 1)
                        finally:
                            await cancel(task)
                        retry.assert_not_awaited()
                finally:
                    await runner.cleanup()
                self.assertEqual(len(clients), 2)
                self.assertTrue(all(client.closed for client in clients))
                self.assertTrue(all(session.closed for session in sessions))
                self.assertTrue(all(ws.closed for ws in websockets))
                self.assertTrue(all(task.done() for task in keepalives))
                database.open.assert_awaited_once()
                database.close.assert_awaited_once()

    async def test_remote_close_reauthenticates_and_reapplies_startup(self):
        await self._verify_two_sessions("remote")

    async def test_keepalive_failure_reinitializes_replacement(self):
        await self._verify_two_sessions("keepalive")

    async def _verify_two_sessions(self, mode):
        clients = []
        received = []
        challenges = []
        handler_errors = []
        summaries = []
        finished = asyncio.Event()
        attempted_keepalives = []
        expected = [INITIALIZE, FILTER_QUERY, EVENT_FILTER, *POST_FILTER_COMMANDS]

        async def handler(request):
            if "Authorization" not in request.headers:
                challenges.append(True)
                return web.Response(status=401, headers={
                    "WWW-Authenticate": 'Digest realm="test", nonce="nonce", algorithm=MD5'
                })
            websocket = web.WebSocketResponse(protocols=("sysmonitor",))
            await websocket.prepare(request)
            messages = []
            received.append(messages)
            number = len(received)
            try:
                messages.append(await websocket.receive_bytes())
                await websocket.send_bytes(struct.pack(
                    "!IIII", 1, 2, INITIALIZE_COMPLETE_EVENT, 0
                ))
                for _ in expected[1:]:
                    messages.append(await websocket.receive_bytes())
                await websocket.send_bytes(struct.pack(
                    "!IIII", 2, 2, FILTER_COMPLETE_EVENT, 0
                ))
                await websocket.send_bytes(CAPTURED_PAYLOAD)
                if number == 1 and mode == "remote":
                    await websocket.close()
                else:
                    async for _ in websocket:
                        pass
            except Exception as error:
                handler_errors.append(error)
            return websocket

        test = self

        class Client(IpoClient):
            def __init__(self, *args, **kwargs):
                if clients:
                    previous = clients[-1]
                    test.assertIsNone(previous._session)
                    test.assertIsNone(previous._websocket)
                    test.assertIsNone(previous._keepalive_task)
                    test.assertTrue(previous.keepalive.done())
                    test.assertTrue(previous.session.closed)
                super().__init__(*args, **kwargs)
                clients.append(self)
                self.patcher = None

            async def connect(self):
                await super().connect()
                self.keepalive = self._keepalive_task
                self.session = self._session
                if len(clients) == 1 and mode == "keepalive":
                    original_send = self._websocket.send_bytes

                    async def send_bytes(payload):
                        if payload == KEEPALIVE:
                            attempted_keepalives.append(payload)
                            raise ConnectionError("injected keepalive send failure")
                        await original_send(payload)

                    self.patcher = patch.object(self._websocket, "send_bytes", send_bytes)
                    self.patcher.start()

            async def close(self):
                try:
                    await super().close()
                finally:
                    if self.patcher is not None:
                        self.patcher.stop()

        original_format = cli._format_summary

        def format_summary(item, verbose):
            summaries.append(item)
            if len(summaries) == 2:
                finished.set()
            return original_format(item, verbose)

        async def retry_wait(seconds):
            self.assertEqual(seconds, 1)
            self.assertIsNone(clients[-1]._session)
            await asyncio.sleep(0)

        application = web.Application()
        application.router.add_get("/sysmonitor/", handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            args = arguments(
                "--port", str(port), "--keepalive-interval", "0.01", "127.0.0.1"
            )
            with (
                patch.object(cli, "IpoClient", Client),
                patch.object(cli, "_retry_wait", retry_wait),
                patch.object(cli, "_format_summary", format_summary),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                task = asyncio.create_task(cli.run(args))
                try:
                    await asyncio.wait_for(finished.wait(), 2)
                    self.assertEqual(len(clients), 2)
                    self.assertFalse(clients[-1].keepalive.done())
                finally:
                    await cancel(task)
        finally:
            await runner.cleanup()
        self.assertEqual(handler_errors, [])
        self.assertEqual(received, [expected, expected])
        self.assertEqual(len(challenges), 2)
        self.assertEqual(len(attempted_keepalives), int(mode == "keepalive"))
        for client in clients:
            self.assertTrue(client.keepalive.done())
            self.assertTrue(client.session.closed)
            self.assertIsNone(client._websocket)
            self.assertEqual(client._keepalive_interval, 0.01)

    async def test_persistence_filters_rotation_and_retention_across_sessions(self):
        databases = []
        completed = asyncio.Event()
        attempts = []
        output = io.StringIO()
        written = []

        class Database(SummaryDatabase):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.opens = self.closes = 0
                databases.append(self)

            async def open(self):
                self.opens += 1
                await super().open()

            async def close(self):
                self.closes += 1
                await super().close()

            async def write(self, item):
                written.append(item)
                await super().write(item)

        class Client:
            def __init__(self, url, **kwargs):
                self.websocket_url = url
                self.number = len(attempts) + 1
                attempts.append(self)

            async def connect(self):
                pass

            async def close(self):
                pass

            async def rtp_summaries(self):
                item = parse_rtp_end(
                    CAPTURED_PAYLOAD, datetime(2026, 1, self.number, 12).astimezone()
                )
                yield item  # Good calls must not be printed or stored.
                yield replace(item, recv=0, sent=self.number)

        async def retry_wait(seconds):
            if len(attempts) == 3:
                completed.set()
                await asyncio.Event().wait()
            await asyncio.sleep(0)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rtp.db"
            old = Path(directory) / "rtp_20251231.db"
            old.write_text("older archive", encoding="utf-8")
            args = arguments(
                "--db", str(path), "--rotate", "1", "--bad-only", "--verbose",
                "192.0.2.1",
            )
            with (
                patch.object(cli, "SummaryDatabase", Database),
                patch.object(cli, "IpoClient", Client),
                patch.object(cli, "_retry_wait", retry_wait),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                task = asyncio.create_task(cli.run(args))
                try:
                    await asyncio.wait_for(completed.wait(), 2)
                    self.assertEqual(len(databases), 1)
                    self.assertEqual(databases[0].opens, 1)
                    self.assertEqual(databases[0].closes, 0)
                finally:
                    await cancel(task)
            self.assertEqual(databases[0].closes, 1)
            self.assertEqual(len(written), 3)
            self.assertEqual(output.getvalue().count("Ulaw64K"), 3)
            for item in written:
                self.assertIn(cli._format_summary(item, True), output.getvalue())
            self.assertEqual(
                {p.name for p in Path(directory).iterdir()},
                {"rtp.db", "rtp_20260102.db"},
            )
            for name, sent in (("rtp.db", 3), ("rtp_20260102.db", 2)):
                with sqlite3.connect(Path(directory, name)) as connection:
                    self.assertEqual(
                        connection.execute("SELECT sent, recv FROM rtp_summaries").fetchall(),
                        [(sent, 0)],
                    )

    async def test_reporting_header_after_connections_and_credential_redaction(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                attempts = {}
                barrier = asyncio.Barrier(2)
                finished = asyncio.Event()
                waits = []
                output, errors = io.StringIO(), io.StringIO()

                class Client:
                    def __init__(self, url, **kwargs):
                        self.websocket_url = url
                        attempts[url] = attempts.get(url, 0) + 1
                        self.number = attempts[url]
                        self.healthy = "192.0.2.2" in url

                    async def connect(self):
                        if self.number == 1:
                            await barrier.wait()
                        elif self.number == 2:
                            raise IpoConnectionError(
                                self.websocket_url,
                                "rejected monitor-user and monitor-password",
                            )

                    async def close(self):
                        pass

                    async def rtp_summaries(self):
                        yield parse_rtp_end(CAPTURED_PAYLOAD, datetime.now(UTC))
                        if self.healthy:
                            await asyncio.Event().wait()
                        elif self.number == 3:
                            raise ConnectionError("lost monitor-password for monitor-user")

                async def retry_wait(seconds):
                    waits.append(seconds)
                    if len(waits) == 3:
                        finished.set()
                        await asyncio.Event().wait()
                    await asyncio.sleep(0)

                args = arguments(
                    *(["--verbose"] if verbose else []),
                    "192.0.2.1", "192.0.2.2",
                )
                with (
                    patch.object(cli, "IpoClient", Client),
                    patch.object(cli, "_retry_wait", retry_wait),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                    self.assertLogs(cli.LOGGER, level="DEBUG") as logs,
                ):
                    task = asyncio.create_task(cli.run(args))
                    try:
                        await asyncio.wait_for(finished.wait(), 1)
                    finally:
                        await cancel(task)
                self.assertEqual(waits, [1, 2, 1])
                self.assertEqual(output.getvalue().count("Date/Time"), 3)
                self.assertEqual(output.getvalue().count("Connected to"), 3)
                self.assertEqual(output.getvalue().count("Ulaw64K"), 3)
                self.assertIn(cli._summary_header(verbose), output.getvalue())
                lines = output.getvalue().splitlines()
                for number, line in enumerate(lines):
                    if line.startswith("Connected to "):
                        self.assertEqual(
                            lines[number + 1], cli._summary_header(verbose)
                        )
                self.assertLess(output.getvalue().index("Date/Time"), output.getvalue().index("Ulaw64K"))
                self.assertIn("Disconnected from http://192.0.2.1:80", errors.getvalue())
                self.assertIn("retrying in 2 seconds", errors.getvalue())
                self.assertIn("[redacted]", errors.getvalue())
                reports = output.getvalue() + errors.getvalue() + "\n".join(logs.output)
                for secret in ("monitor-user", "monitor-password"):
                    self.assertNotIn(secret, reports)

    async def test_local_processing_failures_are_fatal(self):
        for failure in ("database", "stdout", "formatting"):
            with self.subTest(failure=failure):
                clients = []
                barrier = asyncio.Barrier(2)

                class Client:
                    def __init__(self, url, **kwargs):
                        self.websocket_url = url
                        self.closed = False
                        self.stream_closed = False
                        clients.append(self)

                    async def connect(self):
                        pass

                    async def close(self):
                        self.closed = True

                    async def rtp_summaries(self):
                        try:
                            await barrier.wait()
                            if "192.0.2.1" in self.websocket_url:
                                yield parse_rtp_end(CAPTURED_PAYLOAD, datetime.now(UTC))
                            await asyncio.Event().wait()
                        finally:
                            self.stream_closed = True

                class Output(io.StringIO):
                    def write(self, text):
                        if failure == "stdout" and "Ulaw64K" in text:
                            raise OSError("output unavailable")
                        return super().write(text)

                database = MagicMock()
                database.open = AsyncMock()
                database.close = AsyncMock()
                database.write = AsyncMock(
                    side_effect=OSError("storage unavailable")
                    if failure == "database" else None
                )
                original_format = cli._format_summary

                def format_summary(*args):
                    if failure == "formatting":
                        raise ValueError("cannot format summary")
                    return original_format(*args)

                with (
                    patch.object(cli, "IpoClient", Client),
                    patch.object(cli, "SummaryDatabase", return_value=database),
                    patch.object(cli, "_retry_wait", new_callable=AsyncMock) as retry,
                    patch.object(cli, "_format_summary", format_summary),
                    contextlib.redirect_stdout(Output()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(ExceptionGroup):
                        await asyncio.wait_for(
                            cli.run(arguments("--db", "unused.db", "192.0.2.1", "192.0.2.2")),
                            1,
                        )
                    retry.assert_not_awaited()
                self.assertEqual(len(clients), 2)
                self.assertTrue(all(c.closed and c.stream_closed for c in clients))
                database.open.assert_awaited_once()
                database.close.assert_awaited_once()

    async def test_backoff_cap_reset_and_healthy_target_isolation(self):
        attempts = []
        delays = []
        finished = asyncio.Event()
        healthy_received = asyncio.Event()

        class Client:
            def __init__(self, url, **kwargs):
                self.websocket_url = url
                self.healthy = "192.0.2.2" in url
                self.closed = False
                self.number = 1 + sum(not item.healthy for item in attempts)
                attempts.append(self)

            async def connect(self):
                if not self.healthy and self.number != 10:
                    raise IpoConnectionError(self.websocket_url, "offline")

            async def rtp_summaries(self):
                if self.healthy:
                    yield parse_rtp_end(CAPTURED_PAYLOAD, datetime.now(UTC))
                    healthy_received.set()
                    await asyncio.Event().wait()

            async def close(self):
                self.closed = True

        async def retry_wait(seconds):
            self.assertTrue(all(item.closed for item in attempts if not item.healthy))
            delays.append(seconds)
            if len(delays) == 11:
                finished.set()
                await asyncio.Event().wait()
            await asyncio.sleep(0)

        with (
            patch.object(cli, "IpoClient", Client),
            patch.object(cli, "_retry_wait", retry_wait),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            task = asyncio.create_task(cli.run(arguments("192.0.2.1", "192.0.2.2")))
            try:
                await asyncio.wait_for(finished.wait(), 1)
                self.assertTrue(healthy_received.is_set())
                self.assertFalse(task.done())
                self.assertEqual(sum(item.healthy for item in attempts), 1)
                self.assertEqual(delays, [1, 2, 4, 8, 16, 32, 60, 60, 60, 1, 2])
            finally:
                await cancel(task)
        self.assertTrue(all(item.closed for item in attempts))

    async def test_all_offline_remains_running_and_cancels_maximum_wait(self):
        waiting = asyncio.Event()
        attempts = []
        delays = []

        class Client:
            def __init__(self, url, **kwargs):
                self.websocket_url = url
                self.closed = False
                attempts.append(self)

            async def connect(self):
                raise IpoConnectionError(self.websocket_url, "offline")

            async def close(self):
                self.closed = True

        async def retry_wait(seconds):
            delays.append(seconds)
            if seconds == 60:
                waiting.set()
                await asyncio.sleep(seconds)
            else:
                await asyncio.sleep(0)

        with (
            patch.object(cli, "IpoClient", Client),
            patch.object(cli, "_retry_wait", retry_wait),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            task = asyncio.create_task(cli.run(arguments("192.0.2.1")))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                self.assertFalse(task.done())
                self.assertEqual(delays, [1, 2, 4, 8, 16, 32, 60])
            finally:
                await cancel(task)
        self.assertEqual(len(attempts), 7)
        self.assertTrue(all(item.closed for item in attempts))


class ExitStatusTests(unittest.TestCase):
    def test_keyboard_interrupt_exits_130(self):
        with patch.object(cli, "run", new_callable=AsyncMock) as run:
            run.side_effect = KeyboardInterrupt()
            self.assertEqual(cli.main(["192.0.2.1"]), 130)

    def test_fatal_processing_group_exits_nonzero(self):
        with (
            patch.object(cli, "run", new_callable=AsyncMock) as run,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            run.side_effect = ExceptionGroup("monitoring failed", [OSError("storage unavailable")])
            self.assertEqual(cli.main(["192.0.2.1"]), 1)
