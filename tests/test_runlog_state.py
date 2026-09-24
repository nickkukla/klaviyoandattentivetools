import csv

import pytest

from migtool.output import new_run
from migtool.runlog import RunLog
from migtool.state import StateStore


def test_errors_go_to_file_and_set_exit_code(tmp_path):
    shown = []
    log = RunLog(new_run("klaviyo_us", "profiles-import", base=tmp_path), echo=shown.append)
    log.read(3)
    log.written(1)
    log.skipped(1)
    log.error("a@example.com", "invalid email", stage="import")
    assert log.finish() == 1
    assert "read 3, written 1, skipped 1, failed 1" in shown[0]
    with open(log.errors_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["identifier"] == "a@example.com"
    assert rows[0]["stage"] == "import"


def test_clean_run_exits_zero_and_writes_no_error_file(tmp_path):
    log = RunLog(new_run("klaviyo_us", "x", base=tmp_path), echo=lambda _: None)
    log.written(5)
    assert log.finish() == 0
    assert not log.errors_path.exists()


def test_jobs_are_saved_and_updated(tmp_path):
    store = StateStore(tmp_path)
    store.add_job("attentive_us", {"id": "j1", "segment": "VIP-CA", "status": "PENDING"})
    store.update_job("attentive_us", "j1", status="COMPLETE")
    assert store.jobs("attentive_us") == [{"id": "j1", "segment": "VIP-CA", "status": "COMPLETE"}]
    with pytest.raises(ValueError):
        store.add_job("attentive_us", {"id": "j1"})
    with pytest.raises(KeyError):
        store.update_job("attentive_us", "nope", status="x")
