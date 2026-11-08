"""Daily copies of the database, so one bad disk or mistake never loses everything.

SQLite's backup API copies a consistent snapshot while the app keeps writing. Each copy is
checked before it replaces anything, and only the newest `backup_keep` are kept. Set
`NEUROVEX_BACKUP_DIR` to a synced folder to keep them off this computer too.

To restore: stop Neurovex, copy a backup over `neurovex.db` in the data folder (delete the
`-wal` and `-shm` files next to it), and start it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from neurovex.core.config import Settings
from neurovex.core.logging import get_logger

log = get_logger(__name__)

PREFIX = "neurovex-"
EVERY = timedelta(days=1)
FIRST_DELAY_S = 120  # let startup and the first news run settle
MAX_SLEEP_S = 3600  # re-check hourly, so a computer that slept still backs up on time


def list_backups(folder: Path) -> list[Path]:
    """Backups in `folder`, newest first."""
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{PREFIX}*.db"), key=lambda p: p.name, reverse=True)


def last_backup_at(folder: Path) -> datetime | None:
    found = list_backups(folder)
    return datetime.fromtimestamp(found[0].stat().st_mtime, UTC) if found else None


def backup_now(db_path: Path, folder: Path, keep: int, now: datetime | None = None) -> Path:
    """Copy the database to `folder`, verify the copy, and drop backups beyond `keep`."""
    if not db_path.is_file():
        raise FileNotFoundError(f"No database at {db_path}")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    target = folder / f"{PREFIX}{stamp}.db"
    partial = folder / f"{PREFIX}{stamp}.part"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)
            verdict = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if verdict != "ok":
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"Backup failed its check: {verdict}")
    partial.replace(target)
    for old in list_backups(folder)[max(keep, 1) :]:
        old.unlink(missing_ok=True)
    return target


class BackupModule:
    """Makes a backup once a day while the app runs (and soon after start if one is due)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.settings.backup_keep > 0:
            self._task = asyncio.create_task(self._loop(), name="backups")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def seconds_until_due(self, now: datetime | None = None) -> float:
        last = last_backup_at(self.settings.backups_dir)
        if last is None:
            return 0.0
        return max((last + EVERY - (now or datetime.now(UTC))).total_seconds(), 0.0)

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_DELAY_S)
        while True:
            wait = self.seconds_until_due()
            if wait > 0:
                await asyncio.sleep(min(wait, MAX_SLEEP_S))
                continue
            try:
                path = await asyncio.to_thread(
                    backup_now,
                    self.settings.db_path,
                    self.settings.backups_dir,
                    self.settings.backup_keep,
                )
                log.info("backup_done", file=path.name)
            except Exception as e:  # never take the app down; try again in an hour
                log.warning("backup_failed", error=str(e)[:300])
                await asyncio.sleep(MAX_SLEEP_S)
