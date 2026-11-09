"""Tests for SQLite summary persistence and rotation."""

import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from rtpmonIPO.db import SummaryDatabase
from rtpmonIPO.models import RtpSummary


def summary(timestamp: str, sent: int) -> RtpSummary:
    return RtpSummary(
        timestamp,
        "0x01",
        "1",
        "192.0.2.1",
        1000,
        "192.0.2.2",
        2000,
        "G711",
        20,
        "on",
        1,
        sent,
        2,
        0,
        0,
        0,
        0,
        0,
    )


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_append_restart_rotation_and_existing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rtp.db"
            # Naive timestamps are interpreted in the host's local timezone.
            day_one = summary("2026-01-01T12:00:00", 1)
            day_two = replace(day_one, timestamp="2026-01-02T12:00:00", sent=3)
            database = SummaryDatabase(path)
            await database.open()
            await asyncio.gather(
                database.write(day_one), database.write(day_one)
            )
            await database.close()

            database = SummaryDatabase(path)
            await database.open()
            await database.write(day_two)
            await database.close()
            archive = path.with_name("rtp_20260101.db")
            with sqlite3.connect(archive) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM rtp_summaries"
                    ).fetchone()[0],
                    2,
                )
            with sqlite3.connect(path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT sent FROM rtp_summaries"
                    ).fetchall(),
                    [(3,)],
                )

            # Reusing an earlier archive must append instead of overwriting it.
            path.unlink()
            database = SummaryDatabase(path)
            await database.open()
            await database.write(day_one)
            await database.write(day_two)
            await database.close()
            with sqlite3.connect(archive) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM rtp_summaries"
                    ).fetchone()[0],
                    3,
                )
