"""Export files: `exports/<instance>/<object>/<timestamp>.csv` plus `manifest.json`.

CSV is the default. Nested values go into a cell as JSON text. JSONL is only
for data too complex for a usable CSV. `ResumableExport` adds `--resume`
support on top of the writers, and can stage rows as JSONL when the CSV
columns aren't known until the end.
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


def parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 timestamp; one without a zone is taken as UTC."""
    dt = datetime.fromisoformat(value.strip())
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def iso_or_none(value: str | None) -> str | None:
    """An API timestamp in this tool's format (`...Z`), or None when blank."""
    return iso(parse_iso(value)) if value else None


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


def _last_occurrences(rows: Iterable[Mapping[str, Any]], key: list[str]) -> set[int]:
    """Indexes of the last row for each value of `key`."""
    last: dict[tuple, int] = {}
    for i, row in enumerate(rows):
        last[tuple(row.get(k) for k in key)] = i
    return set(last.values())


def _read_rows(path: Path) -> Iterable[dict[str, Any]]:
    with open(path, encoding="utf-8", newline="") as f:
        if path.suffix == ".jsonl":
            for line in f:
                yield json.loads(line)
        else:
            yield from csv.DictReader(f)


TYPE_SUFFIXES = ("number", "bool", "json", "text")


def column_type(values: set[type]) -> str:
    """How a column of JSON values is written: `text` when every value is a
    string, `number`, `bool`, else `json` (lists, objects and mixed types)."""
    if values <= {str}:
        return "text"
    if values <= {int, float}:
        return "number"
    if values == {bool}:
        return "bool"
    return "json"


def typed_cell(value: Any, kind: str) -> str:
    if value is None:
        return ""
    if kind == "json":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return cell(value)


def finalize_csv(
    src: Path, dest: Path, first_columns: Iterable[str], *,
    unique_by: list[str] | None = None, typed_prefix: str | None = None,
) -> tuple[int, int]:
    """Write the rows in `src` (JSONL or CSV) to CSV at `dest`. `src` is left
    in place for the caller to delete once the run is recorded.

    Columns are `first_columns`, then every other key seen, sorted. With
    `unique_by`, only the last row for each key is kept: paging over data that
    changes during an export can return a record twice, and the later copy is
    the fresher one.

    With `typed_prefix` (JSONL sources), columns under that prefix whose values
    aren't all strings get a `#number`, `#bool` or `#json` suffix, and `#json`
    cells are always JSON, so the importer can restore each type exactly
    instead of guessing from the text. Returns (rows written, duplicates dropped).
    """
    first = list(first_columns)
    seen = set(first)
    extra: set[str] = set()
    types: dict[str, set[type]] = {}
    for row in _read_rows(src):
        extra.update(k for k in row if k not in seen)
        if typed_prefix:
            for k, v in row.items():
                if k.startswith(typed_prefix) and v is not None:
                    types.setdefault(k, set()).add(type(v))
    kinds = {k: column_type(t) for k, t in types.items()}

    def column_name(k: str) -> str:
        kind = kinds.get(k, "text")
        if kind != "text":
            return f"{k}#{kind}"
        # A text property whose own name ends in a type suffix is marked #text,
        # so it can't be read as that type or collide with a typed column.
        if typed_prefix and k.startswith(typed_prefix) and k.rpartition("#")[2] in TYPE_SUFFIXES:
            return f"{k}#text"
        return k

    header = {k: column_name(k) for k in first + sorted(extra)}
    keep = _last_occurrences(_read_rows(src), unique_by) if unique_by else None
    tmp = dest.with_name(dest.name + ".tmp")
    writer = CsvWriter(tmp, list(header.values()))
    dropped = 0
    for i, row in enumerate(_read_rows(src)):
        if keep is not None and i not in keep:
            dropped += 1
            continue
        writer.write({header[k]: (typed_cell(v, kinds[k]) if k in kinds else v) for k, v in row.items()})
    writer.close()
    os.replace(tmp, dest)
    return writer.count, dropped


class ResumeError(Exception):
    """`--resume` was asked for but there is nothing matching to resume."""


class ResumableExport:
    """An export that saves its position after each page and can be resumed.

    Call `write` for each row of a page, then `checkpoint(cursor)` with the
    cursor for the *next* page. On `--resume`, the output file is cut back to
    the last checkpoint, so rows written after it are neither lost nor
    duplicated. `params` (filters such as `--since`) must match on resume.

    With `staged=True`, rows go to a `.jsonl` file and `finish` turns it into
    the CSV, with `fieldnames` first and any other keys after them, sorted.
    Use it when rows carry columns that can't be listed up front.

    With `unique_by`, `finish` keeps only the last row for each key, and with
    `typed_prefix`, columns under it carry their type (see `finalize_csv`).
    Both need `staged=True`.
    """

    def __init__(
        self,
        instance: str,
        obj: str,
        fieldnames: Iterable[str],
        *,
        resume: bool = False,
        staged: bool = False,
        unique_by: list[str] | None = None,
        typed_prefix: str | None = None,
        params: Mapping[str, Any] | None = None,
        store: StateStore | None = None,
        base: Path = EXPORTS_DIR,
        now: datetime | None = None,
        echo: Callable[[str], None] = typer.echo,
    ) -> None:
        self.instance = instance
        self.object = obj
        self.params = dict(params or {})
        self.unique_by = unique_by
        self.typed_prefix = typed_prefix
        if (unique_by or typed_prefix) and not staged:
            raise ValueError("unique_by and typed_prefix need staged=True")
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
            self.staged = saved.get("staged", False)
            self.writer = self._open_writer(truncate_to=saved["offset"])
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
            self.staged = staged
            self.writer = self._open_writer()
            self.cursor = None
            self.rows = 0
            self.checkpoint(None)

    def _open_writer(self, truncate_to: int | None = None) -> CsvWriter | JsonlWriter:
        if self.staged:
            return JsonlWriter(self.run.path(".jsonl"), truncate_to=truncate_to)
        return CsvWriter(self.run.path(".csv"), self.fieldnames, truncate_to=truncate_to)

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
                "staged": self.staged,
                "offset": self.writer.flush(),
                "cursor": cursor,
                "rows": self.rows,
            },
        )

    def finish(self, counts: Mapping[str, int] | None = None) -> Path:
        """Close the file, record the run in the manifest and drop the checkpoint.

        The staging file is deleted last, so an interruption at any point leaves
        either a resumable checkpoint with its staging file, or a finished run."""
        self.writer.close()
        path = self.run.path(".csv")
        dropped = 0
        if self.staged:
            self.rows, dropped = finalize_csv(
                self.writer.path, path, self.fieldnames,
                unique_by=self.unique_by, typed_prefix=self.typed_prefix,
            )
        counts = {"rows": self.rows, **(counts or {})}
        if dropped:
            counts["duplicates_dropped"] = dropped
        manifest = write_manifest(
            self.run,
            files={path.name: self.rows},
            counts=counts,
            extra={"params": self.params} if self.params else None,
        )
        self.store.clear_checkpoint(self.instance, self.object)
        if self.staged:
            self.writer.path.unlink(missing_ok=True)
        return manifest
