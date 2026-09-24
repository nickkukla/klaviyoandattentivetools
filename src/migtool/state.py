"""Files under `state/`: export checkpoints for `--resume`, and saved bulk job IDs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class StateStore:
    def __init__(self, base: Path = Path("state")) -> None:
        self.base = base

    def _checkpoint_path(self, instance: str, key: str) -> Path:
        return self.base / instance / f"{key}.checkpoint.json"

    def load_checkpoint(self, instance: str, key: str) -> dict[str, Any] | None:
        path = self._checkpoint_path(instance, key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save_checkpoint(self, instance: str, key: str, data: dict[str, Any]) -> None:
        write_json_atomic(self._checkpoint_path(instance, key), data)

    def clear_checkpoint(self, instance: str, key: str) -> None:
        self._checkpoint_path(instance, key).unlink(missing_ok=True)

    def _jobs_path(self, instance: str) -> Path:
        return self.base / instance / "jobs.json"

    def jobs(self, instance: str) -> list[dict[str, Any]]:
        path = self._jobs_path(instance)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def add_job(self, instance: str, job: dict[str, Any]) -> None:
        """Save a bulk job. `job` must have an `id`."""
        jobs = self.jobs(instance)
        if any(j["id"] == job["id"] for j in jobs):
            raise ValueError(f"Job {job['id']} is already saved for {instance}")
        write_json_atomic(self._jobs_path(instance), [*jobs, job])

    def update_job(self, instance: str, job_id: str, **fields: Any) -> None:
        jobs = self.jobs(instance)
        for job in jobs:
            if job["id"] == job_id:
                job.update(fields)
                break
        else:
            raise KeyError(f"No saved job {job_id} for {instance}")
        write_json_atomic(self._jobs_path(instance), jobs)
