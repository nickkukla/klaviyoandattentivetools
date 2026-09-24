import csv
import json
from datetime import UTC, datetime

import pytest

from migtool.output import CsvWriter, ResumableExport, ResumeError, cell, iso, new_run
from migtool.state import StateStore

FIELDS = ["id", "email", "properties.tags"]
ROWS = [{"id": str(i), "email": f"p{i}@example.com", "properties.tags": ["a", i]} for i in range(25)]


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def make(tmp_path, **kwargs):
    return ResumableExport(
        "klaviyo_ca", "profiles", FIELDS, store=StateStore(tmp_path / "state"),
        base=tmp_path / "exports", echo=lambda _: None, **kwargs,
    )


def export(exp, crash_after=None):
    """Pages of 10 rows; the cursor is the index of the next row."""
    written = 0
    cursor = exp.cursor or 0
    while cursor < len(ROWS):
        for row in ROWS[cursor:cursor + 10]:
            exp.write(row)
            written += 1
            if crash_after is not None and written == crash_after:
                exp.writer.flush()  # rows reached disk, but no checkpoint yet
                raise RuntimeError("crash")
        cursor += 10
        exp.checkpoint(cursor)
    return exp.finish()


def test_cells():
    assert cell(None) == ""
    assert cell(True) == "true"
    assert cell({"b": 1, "a": "é"}) == '{"b":1,"a":"é"}'
    assert cell(datetime(2026, 9, 24, 15, 30, tzinfo=UTC)) == "2026-09-24T15:30:00Z"


def test_iso_is_utc_with_z():
    assert iso(datetime(2026, 9, 24, 15, 30, 5, tzinfo=UTC)) == "2026-09-24T15:30:05Z"


def test_run_paths(tmp_path):
    run = new_run("klaviyo_ca", "profiles", base=tmp_path, now=datetime(2026, 9, 24, 15, 30, tzinfo=UTC))
    assert run.path(".csv") == tmp_path / "klaviyo_ca" / "profiles" / "20260924T153000Z.csv"
    assert run.run_id.startswith("20260924T153000Z-")


def test_writer_rejects_unknown_columns(tmp_path):
    w = CsvWriter(tmp_path / "x.csv", ["a"])
    with pytest.raises(ValueError, match="not in the header"):
        w.write({"a": 1, "b": 2})


def test_full_export_writes_manifest_and_clears_checkpoint(tmp_path):
    exp = make(tmp_path)
    manifest = json.loads(export(exp).read_text())
    assert len(read_csv(exp.writer.path)) == 25
    entry = manifest["runs"][0]
    assert entry["counts"]["rows"] == 25
    assert entry["files"] == {exp.writer.path.name: 25}
    assert StateStore(tmp_path / "state").load_checkpoint("klaviyo_ca", "profiles") is None


def test_resume_after_crash_has_no_duplicates_or_gaps(tmp_path):
    first = make(tmp_path, params={"since": "2026-09-01T00:00:00Z"})
    with pytest.raises(RuntimeError):
        export(first, crash_after=15)  # page 1 checkpointed, 5 rows of page 2 not
    first.writer.close()

    resumed = make(tmp_path, resume=True, params={"since": "2026-09-01T00:00:00Z"})
    assert resumed.run.run_id == first.run.run_id
    assert resumed.rows == 10
    manifest = json.loads(export(resumed).read_text())

    rows = read_csv(first.writer.path)
    assert [r["id"] for r in rows] == [str(i) for i in range(25)]
    assert json.loads(rows[3]["properties.tags"]) == ["a", 3]
    assert len(manifest["runs"]) == 1
    assert manifest["runs"][0]["counts"]["rows"] == 25


def test_resume_needs_an_unfinished_export(tmp_path):
    with pytest.raises(ResumeError, match="No unfinished"):
        make(tmp_path, resume=True)


def test_resume_needs_the_same_options(tmp_path):
    make(tmp_path, params={"segment": "VIP"})
    with pytest.raises(ResumeError, match="different options"):
        make(tmp_path, resume=True, params={"segment": "Lapsed"})


def test_new_export_abandons_unfinished_one_with_a_warning(tmp_path):
    make(tmp_path)
    messages = []
    ResumableExport("klaviyo_ca", "profiles", FIELDS, store=StateStore(tmp_path / "state"),
                    base=tmp_path / "exports", echo=messages.append)
    assert "use --resume" in messages[0]


def test_unique_by_keeps_the_last_copy(tmp_path):
    exp = ResumableExport(
        "klaviyo_ca", "profiles", ["id", "email"], staged=True, unique_by=["id"],
        store=StateStore(tmp_path / "state"), base=tmp_path / "exports", echo=lambda _: None,
    )
    for row in ({"id": "1", "email": "old@example.com"}, {"id": "2", "email": "b@example.com"},
                {"id": "1", "email": "new@example.com", "properties.x": "y"}):
        exp.write(row)
    exp.checkpoint(None)
    manifest = json.loads(exp.finish().read_text())
    rows = read_csv(exp.run.path(".csv"))
    assert [(r["id"], r["email"]) for r in rows] == [("2", "b@example.com"), ("1", "new@example.com")]
    assert list(rows[0]) == ["id", "email", "properties.x"]
    assert manifest["runs"][-1]["counts"] == {"rows": 2, "duplicates_dropped": 1}
