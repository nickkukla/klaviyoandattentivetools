"""Per-run error file, end-of-run summary and exit code."""

from __future__ import annotations

import csv
from collections.abc import Callable
from typing import TextIO

import typer

from migtool.output import Run, iso, utc_now

ERROR_COLUMNS = ["time", "stage", "identifier", "error"]


class RunLog:
    """Counts read / written / skipped / failed; per-record errors go to
    `<run>.errors.csv` and the run carries on."""

    def __init__(
        self, run: Run, *, echo: Callable[[str], None] = typer.echo, written_label: str = "written"
    ) -> None:
        self.run = run
        self.echo = echo
        self.counts = {"read": 0, "written": 0, "skipped": 0, "failed": 0}
        # "submitted" for suppressions: Klaviyo applies them later, so the
        # count is what was sent, not what's confirmed (see suppressions check).
        self.written_label = written_label
        self.errors_path = run.path(".errors.csv")
        self._errors: TextIO | None = None
        self._writer = None

    def read(self, n: int = 1) -> None:
        self.counts["read"] += n

    def written(self, n: int = 1) -> None:
        self.counts["written"] += n

    def skipped(self, n: int = 1) -> None:
        self.counts["skipped"] += n

    def error(self, identifier: str, message: str, *, stage: str = "") -> None:
        """Record one failed record. Appends, so a resumed run keeps earlier errors."""
        self.counts["failed"] += 1
        if self._writer is None:
            is_new = not self.errors_path.exists()
            self._errors = open(self.errors_path, "a", encoding="utf-8", newline="")
            self._writer = csv.writer(self._errors)
            if is_new:
                self._writer.writerow(ERROR_COLUMNS)
        self._writer.writerow([iso(utc_now()), stage, identifier, message])
        self._errors.flush()

    @property
    def exit_code(self) -> int:
        return 1 if self.counts["failed"] else 0

    def summary(self) -> str:
        return ", ".join(f"{self.written_label if k == 'written' else k} {v:,}" for k, v in self.counts.items())

    def finish(self) -> int:
        """Close the error file, print the summary and return the exit code."""
        if self._errors is not None:
            self._errors.close()
            self._errors = self._writer = None
        self.echo(f"Run {self.run.run_id}: {self.summary()}")
        if self.counts["failed"]:
            self.echo(f"Errors written to {self.errors_path}")
        return self.exit_code
