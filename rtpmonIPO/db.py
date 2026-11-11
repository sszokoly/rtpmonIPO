"""Asynchronous SQLite storage for RTP summaries."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

from .models import RtpSummary

SUMMARY_COLUMNS = (
    "timestamp",
    "ipo_timestamp",
    "sess",
    "local_ip",
    "local_rtp_port",
    "remote_ip",
    "remote_rtp_port",
    "codec",
    "pktsz",
    "rfc2833",
    "age",
    "sent",
    "recv",
    "rtpdelay",
    "jitter",
    "loss",
    "remotejitter",
    "remoteloss",
)
SUMMARY_SCHEMA = """
    (timestamp TEXT NOT NULL, ipo_timestamp TEXT NOT NULL, sess TEXT NOT NULL,
     local_ip TEXT NOT NULL, local_rtp_port INTEGER NOT NULL,
     remote_ip TEXT NOT NULL, remote_rtp_port INTEGER NOT NULL,
     codec TEXT NOT NULL, pktsz INTEGER NOT NULL, rfc2833 TEXT NOT NULL,
     age INTEGER NOT NULL, sent INTEGER NOT NULL, recv INTEGER NOT NULL,
     rtpdelay INTEGER NOT NULL, jitter INTEGER NOT NULL, loss INTEGER NOT NULL,
     remotejitter INTEGER NOT NULL, remoteloss INTEGER NOT NULL)
"""
DEFAULT_ROTATE = 7


class SummaryDatabase:
    """Store summaries in a daily rotating SQLite database."""

    def __init__(self, path: str | Path, *, rotate: int = DEFAULT_ROTATE) -> None:
        if isinstance(rotate, bool) or not isinstance(rotate, int) or rotate < 0:
            raise ValueError("rotate must be a non-negative integer")
        self.path = Path(path)
        self._retained_archives = rotate
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._date: date | None = None

    async def open(self) -> None:
        """Open the database and recover the active date from its contents."""
        if self._connection is not None:
            raise RuntimeError("database is already open")
        try:
            import aiosqlite
        except ImportError as error:
            raise RuntimeError(
                "SQLite storage requires aiosqlite; "
                "install it with pip install 'rtpmonipo[db]'"
            ) from error
        connection = await aiosqlite.connect(self.path)
        try:
            await connection.execute(
                f"CREATE TABLE IF NOT EXISTS rtp_summaries {SUMMARY_SCHEMA}"
            )
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS rtp_metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            async with connection.execute(
                "SELECT value FROM rtp_metadata WHERE key = 'active_date'"
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                self._date = date.fromisoformat(row[0])
            else:
                async with connection.execute(
                    "SELECT timestamp FROM rtp_summaries"
                ) as cursor:
                    async for record in cursor:
                        record_date = _summary_date(record[0])
                        if self._date is None or record_date > self._date:
                            self._date = record_date
            await connection.commit()
        except BaseException:
            await connection.close()
            raise
        self._connection = connection

    async def close(self) -> None:
        """Close the database connection."""
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
                self._date = None

    async def write(self, summary: RtpSummary) -> None:
        """Append a summary, rotating when its local date advances."""
        day = _summary_date(summary.timestamp)
        async with self._lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("database is not open")
            if self._date is not None and day > self._date:
                await self._rotate(connection, self._date)
                self._date = None
                await asyncio.to_thread(self._prune_archives)

            active_date = self._date if self._date is not None else day
            columns = ", ".join(SUMMARY_COLUMNS)
            placeholders = ", ".join("?" for _ in SUMMARY_COLUMNS)
            values = summary.to_dict()
            try:
                await connection.execute(
                    f"INSERT INTO rtp_summaries ({columns}) VALUES ({placeholders})",
                    tuple(values[name] for name in SUMMARY_COLUMNS),
                )
                await connection.execute(
                    "INSERT OR REPLACE INTO rtp_metadata (key, value) "
                    "VALUES ('active_date', ?)",
                    (active_date.isoformat(),),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
            self._date = active_date

    async def _rotate(
        self, connection: aiosqlite.Connection, day: date
    ) -> None:
        archive = self.path.with_name(
            f"{self.path.stem}_{day:%Y%m%d}{self.path.suffix}"
        )
        await connection.execute(
            "ATTACH DATABASE ? AS rtp_archive", (str(archive),)
        )
        try:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS rtp_archive.rtp_summaries "
                + SUMMARY_SCHEMA
            )
            columns = ", ".join(SUMMARY_COLUMNS)
            await connection.execute(
                f"INSERT INTO rtp_archive.rtp_summaries ({columns}) "
                f"SELECT {columns} FROM main.rtp_summaries"
            )
            await connection.execute("DELETE FROM main.rtp_summaries")
            await connection.execute("DELETE FROM main.rtp_metadata")
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.execute("DETACH DATABASE rtp_archive")

    def _prune_archives(self) -> None:
        """Keep the newest dated archives belonging to this database only."""
        prefix = f"{self.path.stem}_"
        suffix = self.path.suffix
        archives: list[tuple[date, Path]] = []
        for path in self.path.parent.iterdir():
            if not path.name.startswith(prefix):
                continue
            tail = path.name[len(prefix):]
            if suffix:
                if not tail.endswith(suffix):
                    continue
                tail = tail[:-len(suffix)]
            if len(tail) != 8 or not tail.isascii() or not tail.isdigit():
                continue
            try:
                day = date(int(tail[:4]), int(tail[4:6]), int(tail[6:]))
            except ValueError:
                continue
            if path.is_file() and not path.is_symlink():
                archives.append((day, path))
        archives.sort(key=lambda item: item[0], reverse=True)
        for _, path in archives[self._retained_archives:]:
            path.unlink(missing_ok=True)


def _summary_date(timestamp: str) -> date:
    """Match the local calendar date used by the CLI output."""
    return datetime.fromisoformat(timestamp).astimezone().date()
