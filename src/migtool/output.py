"""Export files: `exports/<instance>/<object>/<timestamp>.csv` plus `manifest.json`.

CSV is the default. Nested values go into a cell as JSON text. JSONL is only
for data too complex for a usable CSV. `ResumableExport` adds `--resume`
support on top of the writers.
"""

from __future__ import annotations

import csv
import json
import os
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import typer

from migtool.state import StateStore, write_json_atomic

EXPORTS_DIR = Path("exports")


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """UTC ISO 8601 with a `Z`, e.g. `2026-09-24T15:30:00Z`."""
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def file_stamp(dt: datetime) -> str:
    """ISO 8601 basic format for file names (no colons), e.g. `20260924T153000Z`."""
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True)
class Run:
    """One run of one command against one instance and object."""

    instance: str
    object: str
    stamp: str
    run_id: str
    started: str
    dir: Path

    def path(self, suffix: str) -> Path:
        """A file for this run, e.g. `path(".csv")` or `path(".errors.csv")`."""
        return self.dir / f"{self.stamp}{suffix}"


def new_run(
    instance: str, obj: str, *, base: Path = EXPORTS_DIR, now: datetime | None = None
) -> Run:
    now = now or utc_now()
    stamp = file_stamp(now)
    run_dir = base / instance / obj
    run_dir.mkdir(parents=True, exist_ok=True)
    return Run(instance, obj, stamp, f"{stamp}-{secrets.token_hex(2)}", iso(now), run_dir)


def cell(value: Any) -> str:
    """Render one value for a CSV cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _open_for_write(path: Path, truncate_to: int | None) -> tuple[TextIO, bool]:
    """Open `path` for writing; returns (file, is_new). With `truncate_to`, cut the
    file back to that byte offset and append from there."""
    if truncate_to is None:
        return open(path, "w", encoding="utf-8", newline=""), True
    os.truncate(path, truncate_to)
    return open(path, "a", encoding="utf-8", newline=""), False


class CsvWriter:
    def __init__(
        self, path: Path, fieldnames: Iterable[str], *, truncate_to: int | None = None
    ) -> None:
        self.path = path
        self.fieldnames = list(fieldnames)
        self._known = set(self.fieldnames)
        self._f, is_new = _open_for_write(path, truncate_to)
        self._w = csv.writer(self._f)
        if is_new:
            self._w.writerow(self.fieldnames)
        self.count = 0

    def write(self, row: Mapping[str, Any]) -> None:
        extra = set(row) - self._known
        if extra:
            raise ValueError(f"Row has columns not in the header: {sorted(extra)}")
        self._w.writerow([cell(row.get(name)) for name in self.fieldnames])
        self.count += 1

    def flush(self) -> int:
        """Flush to disk and return the byte offset reached."""
        self._f.flush()
        os.fsync(self._f.fileno())
        return self._f.buffer.tell()

    def close(self) -> None:
        self._f.close()


class JsonlWriter:
    def __init__(self, path: Path, *, truncate_to: int | None = None) -> None:
        self.path = path
        self._f, _ = _open_for_write(path, truncate_to)
        self.count = 0

    def write(self, record: Mapping[str, Any]) -> None:
        self._f.write(json.dumps(record, ensure_ascii=False, default=cell) + "\n")
        self.count += 1

    def flush(self) -> int:
        self._f.flush()
        os.fsync(self._f.fileno())
        return self._f.buffer.tell()

    def close(self) -> None:
        self._f.close()


def write_manifest(
    run: Run,
    *,
    files: Mapping[str, int],
    counts: Mapping[str, int],
    status: str = "complete",
    extra: Mapping[str, Any] | None = None,
) -> Path:
    """Record this run in `<object dir>/manifest.json`, replacing any earlier entry
    for the same run (as happens when a run is resumed)."""
    path = run.dir / "manifest.json"
    runs = json.loads(path.read_text(encoding="utf-8"))["runs"] if path.exists() else []
    entry = {
        "run_id": run.run_id,
        "instance": run.instance,
        "object": run.object,
        "started": run.started,
        "finished": iso(utc_now()),
        "status": status,
        "files": dict(files),
        "counts": dict(counts),
        **(extra or {}),
    }
    runs = [r for r in runs if r.get("run_id") != run.run_id] + [entry]
    write_json_atomic(path, {"runs": runs})
    return path


class ResumeError(Exception):
    """`--resume` was asked for but there is nothing matching to resume."""


class ResumableExport:
    """An export that saves its position after each page and can be resumed.

    Call `write` for each row of a page, then `checkpoint(cursor)` with the
    cursor for the *next* page. On `--resume`, the output file is cut back to
    the last checkpoint, so rows written after it are neither lost nor
    duplicated. `params` (filters such as `--since`) must match on resume.
    """

    def __init__(
        self,
        instance: str,
        obj: str,
        fieldnames: Iterable[str],
        *,
        resume: bool = False,
        params: Mapping[str, Any] | None = None,
        store: StateStore | None = None,
        base: Path = EXPORTS_DIR,
        now: datetime | None = None,
        echo: Callable[[str], None] = typer.echo,
    ) -> None:
        self.instance = instance
        self.object = obj
        self.params = dict(params or {})
        self.store = store or StateStore()
        saved = self.store.load_checkpoint(instance, obj)

        if resume:
            if saved is None:
                raise ResumeError(f"No unfinished {obj} export for {instance} to resume.")
            if saved["params"] != self.params:
                raise ResumeError(
                    f"The unfinished {obj} export used different options {saved['params']}; "
                    f"resume it with the same options."
                )
            self.run = Run(
                instance, obj, saved["stamp"], saved["run_id"], saved["started"],
                base / instance / obj,
            )
            self.fieldnames = saved["fieldnames"]
            self.writer = CsvWriter(
                self.run.path(".csv"), self.fieldnames, truncate_to=saved["offset"]
            )
            self.cursor: Any = saved["cursor"]
            self.rows = saved["rows"]
            echo(f"Resuming {obj} export {self.run.run_id} at row {self.rows:,}.")
        else:
            if saved is not None:
                echo(
                    f"Starting a new {obj} export. The unfinished one ({saved['run_id']}) "
                    f"is abandoned; use --resume to continue it instead."
                )
            self.run = new_run(instance, obj, base=base, now=now)
            self.fieldnames = list(fieldnames)
            self.writer = CsvWriter(self.run.path(".csv"), self.fieldnames)
            self.cursor = None
            self.rows = 0
            self.checkpoint(None)

    def write(self, row: Mapping[str, Any]) -> None:
        self.writer.write(row)
        self.rows += 1

    def checkpoint(self, cursor: Any) -> None:
        self.cursor = cursor
        self.store.save_checkpoint(
            self.instance,
            self.object,
            {
                "stamp": self.run.stamp,
                "run_id": self.run.run_id,
                "started": self.run.started,
                "params": self.params,
                "fieldnames": self.fieldnames,
                "offset": self.writer.flush(),
                "cursor": cursor,
                "rows": self.rows,
            },
        )

    def finish(self, counts: Mapping[str, int] | None = None) -> Path:
        """Close the file, record the run in the manifest and drop the checkpoint."""
        self.writer.close()
        manifest = write_manifest(
            self.run,
            files={self.writer.path.name: self.rows},
            counts={"rows": self.rows, **(counts or {})},
            extra={"params": self.params} if self.params else None,
        )
        self.store.clear_checkpoint(self.instance, self.object)
        return manifest
