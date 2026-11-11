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
    async def test_retention_after_rotation(self) -> None:
        for count in (7, 2, 0):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "rtp.db"
                unrelated = [
                    "other_20260101.db", "rtp_20260230.db",
                    "rtp_backup.db", "rtp_20260101.db.bak",
                ]
                for name in unrelated:
                    Path(directory, name).write_text("preserve", encoding="utf-8")
                database = (
                    SummaryDatabase(path) if count == 7
                    else SummaryDatabase(path, rotate=count)
                )
                await database.open()
                try:
                    for day in range(1, 11):
                        await database.write(
                            summary(f"2026-01-{day:02d}T12:00:00", day)
                        )
                        expected = {
                            f"rtp_202601{old:02d}.db"
                            for old in range(max(1, day - count), day)
                        }
                        actual = {
                            item.name for item in Path(directory).iterdir()
                            if item.name not in unrelated and item != path
                        }
                        self.assertEqual(actual, expected)
                        for name in unrelated:
                            self.assertEqual(
                                Path(directory, name).read_text(encoding="utf-8"),
                                "preserve",
                            )
                    with sqlite3.connect(path) as connection:
                        self.assertEqual(
                            connection.execute(
                                "SELECT sent FROM rtp_summaries"
                            ).fetchall(),
                            [(10,)],
                        )
                finally:
                    await database.close()

    def test_invalid_retention(self) -> None:
        for value in (-1, 1.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SummaryDatabase("unused.db", rotate=value)

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
