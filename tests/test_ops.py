"""Running Neurovex day to day: backups, autostart, logging without a console."""

from __future__ import annotations

import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neurovex import cli
from neurovex.core import autostart
from neurovex.core.backup import BackupModule, backup_now, last_backup_at, list_backups

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _db(path: Path, rows: int = 3) -> Path:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)")
    con.executemany("INSERT INTO notes (text) VALUES (?)", [(f"note {i}",) for i in range(rows)])
    con.commit()
    con.close()
    return path


def test_backup_copies_everything_and_keeps_only_the_newest(tmp_path):
    db = _db(tmp_path / "neurovex.db")
    folder = tmp_path / "backups"
    start = datetime(2026, 11, 1, 8, tzinfo=UTC)
    for day in range(4):
        backup_now(db, folder, keep=2, now=start + timedelta(days=day))
    kept = list_backups(folder)
    assert [p.name for p in kept] == ["neurovex-20261104-080000.db", "neurovex-20261103-080000.db"]
    copy = sqlite3.connect(kept[0])
    assert copy.execute("SELECT count(*) FROM notes").fetchone()[0] == 3
    copy.close()
    assert not list(folder.glob("*.part"))


def test_backup_reads_a_database_that_is_open_for_writing(tmp_path):
    db = _db(tmp_path / "neurovex.db")
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO notes (text) VALUES ('unsaved until commit')")  # open transaction
    made = backup_now(db, tmp_path / "b", keep=7)
    writer.commit()
    writer.close()
    copy = sqlite3.connect(made)
    assert copy.execute("SELECT count(*) FROM notes").fetchone()[0] == 3  # a consistent snapshot
    copy.close()


def test_backup_without_a_database_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_now(tmp_path / "missing.db", tmp_path / "b", keep=7)


def test_backups_are_due_daily(settings):
    settings.ensure_dirs()
    module = BackupModule(settings)
    assert module.seconds_until_due() == 0  # none yet: right away
    made = backup_now(_db(settings.data_dir / "neurovex.db"), settings.backups_dir, keep=7)
    last = last_backup_at(settings.backups_dir)
    assert last is not None and made.exists()
    assert module.seconds_until_due(last + timedelta(hours=1)) == pytest.approx(23 * 3600)
    assert module.seconds_until_due(last + timedelta(days=2)) == 0


def test_backup_folder_can_be_moved(settings, tmp_path):
    assert settings.backups_dir == settings.data_dir / "backups"
    synced = settings.model_copy(update={"backup_dir": tmp_path / "OneDrive" / "Neurovex"})
    assert synced.backups_dir == tmp_path / "OneDrive" / "Neurovex"


def test_autostart_task_runs_windowless_from_the_project_at_sign_in():
    xml = autostart.task_xml(
        Path(r"C:\proj\.venv\Scripts\pythonw.exe"), Path(r"C:\proj"), "PC\\Mahi & Co"
    )
    root = ET.fromstring(xml.split("\n", 1)[1])  # skip the UTF-16 declaration
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == "PC\\Mahi & Co"
    action = root.find("t:Actions/t:Exec", NS)
    assert action.find("t:Command", NS).text.endswith("pythonw.exe")
    assert action.find("t:Arguments", NS).text == "-m neurovex serve"
    assert action.find("t:WorkingDirectory", NS).text == r"C:\proj"
    settings = root.find("t:Settings", NS)
    assert (
        settings.find("t:ExecutionTimeLimit", NS).text == "PT0S"
    )  # never stopped for running long
    assert settings.find("t:DisallowStartIfOnBatteries", NS).text == "false"


def test_autostart_registers_through_schtasks(monkeypatch, tmp_path):
    calls: list[tuple[str, ...]] = []
    seen_xml: list[str] = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def fake(*args: str):
        calls.append(args)
        if "/XML" in args:
            seen_xml.append(Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16"))
        return Done()

    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_text("")
    monkeypatch.setattr(autostart, "_schtasks", fake)
    monkeypatch.setattr(autostart, "windowless_python", lambda: pythonw)
    autostart.register()
    assert calls[0][:3] == ("/Create", "/TN", "Neurovex") and calls[0][-1] == "/F"
    assert "<Arguments>-m neurovex serve</Arguments>" in seen_xml[0]
    assert autostart.unregister() is True
    assert [c[0] for c in calls[1:]] == ["/Query", "/End", "/Delete"]


def test_without_a_console_output_goes_to_the_log_file(settings, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    settings.ensure_dirs()
    big = settings.logs_dir / "server.log"
    big.write_bytes(b"x" * (cli.SERVER_LOG_MAX_BYTES + 1))
    cli._log_to_file(settings)
    try:
        print("hello from the background")
    finally:
        sys.stdout.close()
    assert (settings.logs_dir / "server.log.1").stat().st_size > cli.SERVER_LOG_MAX_BYTES
    assert big.read_text(encoding="utf-8").strip() == "hello from the background"
