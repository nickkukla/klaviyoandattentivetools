"""`migtool` command line. Service commands are added phase by phase."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import typer

from migtool.config import INSTANCES, ConfigError, credential, get_instance, load_env
from migtool.http import ApiError
from migtool.klaviyo import bis, groups, imports, profiles, segments, suppressions
from migtool.klaviyo.client import KlaviyoClient
from migtool.klaviyo.writes import Job, Writer
from migtool.output import (
    CsvWriter,
    ResumableExport,
    ResumeError,
    iso,
    iso_or_none,
    new_run,
    parse_iso,
    write_manifest,
)
from migtool.runlog import RunLog
from migtool.safety import WriteRefused, confirm_write
from migtool.state import StateStore

# Locals are hidden in tracebacks so a crash can't print a key.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
klaviyo_app = typer.Typer(no_args_is_help=True, help="Klaviyo exports and imports.")
app.add_typer(klaviyo_app, name="klaviyo")
profiles_app = typer.Typer(no_args_is_help=True, help="Profiles.")
lists_app = typer.Typer(no_args_is_help=True, help="Lists and their members.")
segments_app = typer.Typer(no_args_is_help=True, help="Segments and their members.")
suppressions_app = typer.Typer(no_args_is_help=True, help="Email suppressions.")
bis_app = typer.Typer(no_args_is_help=True, help="Back in Stock signups, for upload to STOQ.")
klaviyo_app.add_typer(profiles_app, name="profiles")
klaviyo_app.add_typer(lists_app, name="lists")
klaviyo_app.add_typer(segments_app, name="segments")
klaviyo_app.add_typer(suppressions_app, name="suppressions")
klaviyo_app.add_typer(bis_app, name="bis")

INSTANCE = typer.Option(..., "--instance", help="Klaviyo instance to export from.")
SINCE = typer.Option(
    None, "--since", help="Only records changed after this UTC ISO 8601 time, e.g. 2026-09-24T15:30:00Z."
)
RESUME = typer.Option(False, "--resume", help="Continue the unfinished export with the same options.")
TO = typer.Option(..., "--to", help="Klaviyo instance to write to.")
FILE = typer.Option(..., "--file", exists=True, dir_okay=False, help="CSV to import.")
LIMIT = typer.Option(None, "--limit", min=1, help="Only the first N rows, for trials and pilots.")
YES = typer.Option(False, "--yes", help="Skip the typed confirmation.")
AS_UNSUBSCRIBE = typer.Option(
    False, "--as-unsubscribe",
    help="Unsubscribe instead of suppressing. Blocks marketing email now, but a later subscribe lifts it.",
)
ALLOW_SOURCE = typer.Option(
    False, "--allow-write-to-source", help="Allow writing to a _ca (source) instance."
)


@app.callback()
def _root() -> None:
    """Klaviyo migration tools."""
    load_env()


@app.command()
def instances() -> None:
    """List every instance and whether its .env variable is set (values are never shown)."""
    for inst in INSTANCES.values():
        status = "set" if os.environ.get(inst.env_var, "").strip() else "NOT SET"
        typer.echo(f"{inst.name:<17} {inst.service:<10} {inst.env_var:<25} {status}")


@klaviyo_app.command("whoami")
def klaviyo_whoami(
    instance: str = typer.Option(..., "--instance", help="Klaviyo instance to check."),
) -> None:
    """Show the account ID and name a key belongs to."""
    inst = get_instance(instance, "klaviyo")
    with KlaviyoClient(credential(inst)) as client:
        acct = client.account()
    typer.echo(f"{inst.name}: account {acct['id']} ({acct['name']})")


def _since(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_iso(value)
    except ValueError as exc:
        raise typer.BadParameter(f"'{value}' is not an ISO 8601 time.", param_hint="--since") from exc


def _client(instance: str) -> KlaviyoClient:
    return KlaviyoClient(credential(get_instance(instance, "klaviyo")))


def _progress(label: str):
    shown = [0]

    def report(rows: int) -> None:
        if rows >= shown[0] + 10_000:
            shown[0] = rows - rows % 10_000
            typer.echo(f"  {label}: {rows:,} rows")

    return report


@profiles_app.command("export")
def profiles_export(
    instance: str = INSTANCE,
    segment: str | None = typer.Option(None, "--segment", help="Only this segment's members (ID or exact name)."),
    since: str | None = SINCE,
    with_predictive: bool = typer.Option(
        False, "--with-predictive", help="Add predictive analytics columns (slower rate limit)."
    ),
    resume: bool = RESUME,
) -> None:
    """Export every profile, whatever its consent, to CSV."""
    since_dt = _since(since)
    with _client(instance) as client:
        segment_id = segment_name = None
        if segment:
            try:
                segment_id, segment_name = profiles.resolve_segment(client, segment)
            except LookupError as exc:
                raise ConfigError(str(exc)) from exc
        params = {"segment": segment_id, "since": since, "with_predictive": with_predictive}
        exp = ResumableExport(
            instance, "profiles", profiles.COLUMNS, resume=resume, staged=True, unique_by=["id"],
            params={k: v for k, v in params.items() if v},
        )
        skipped = profiles.export(
            client, exp, segment_id=segment_id, since=since_dt,
            predictive=with_predictive, progress=_progress("profiles"),
        )
        rows = exp.rows
        exp.finish({"skipped_not_changed": skipped} if skipped else None)
    where = f" in segment {segment_id} ({segment_name})" if segment_id else ""
    typer.echo(f"Exported {rows:,} profiles{where} to {exp.run.path('.csv')}")


@suppressions_app.command("export")
def suppressions_export(instance: str = INSTANCE, since: str | None = SINCE, resume: bool = RESUME) -> None:
    """Export every email suppression with its email, reason and date."""
    since_dt = _since(since)
    with _client(instance) as client:
        exp = ResumableExport(
            instance, "suppressions", suppressions.COLUMNS, resume=resume,
            unique_by=["profile_id", "reason", "timestamp"],
            params={"since": since} if since else None,
        )
        suppressions.export(client, exp, since=since_dt, progress=_progress("suppressions"))
        rows = exp.rows
        exp.finish()
    typer.echo(f"Exported {rows:,} suppressions to {exp.run.path('.csv')}")


def _export_groups(
    instance: str, kind: str, group_columns: list[str], fields: str, since: datetime | None,
    group_row,
) -> None:
    """Shared body of `lists export` and `segments export`."""
    run = new_run(instance, f"{kind}s")
    groups_path, members_path = run.path(f".{kind}s.csv"), run.path(f".{kind}_members.csv")
    with _client(instance) as client:
        found = groups.all_groups(client, kind, fields)
        typer.echo(f"{len(found)} {kind}s")
        member_writer = CsvWriter(members_path, groups.member_columns(kind))
        rows = []
        for group in found:
            before = member_writer.count
            for row in groups.members(client, kind, group, since=since):
                member_writer.write(row)
            rows.append(group_row(client, group, member_writer.count - before))
            typer.echo(f"  {group['attributes']['name']}: {member_writer.count - before:,} members")
        member_writer.close()
    group_writer = CsvWriter(groups_path, group_columns)
    for row in rows:
        group_writer.write(row)
    group_writer.close()
    write_manifest(
        run,
        files={groups_path.name: group_writer.count, members_path.name: member_writer.count},
        counts={kind + "s": group_writer.count, "members": member_writer.count},
        extra={"params": {"since": iso(since)}} if since else None,
    )
    typer.echo(f"Wrote {groups_path} and {members_path}")


@lists_app.command("export")
def lists_export(
    instance: str = INSTANCE,
    since: str | None = typer.Option(
        None, "--since", help="Only memberships that joined after this UTC ISO 8601 time."
    ),
) -> None:
    """Export every list (lists.csv) and its members (list_members.csv)."""

    def row(client, group, count):
        a = group["attributes"]
        return {"id": group["id"], "name": a["name"], "created": iso_or_none(a.get("created")),
                "updated": iso_or_none(a.get("updated")), "opt_in_process": a.get("opt_in_process"),
                "member_count": count}

    _export_groups(
        instance, "list", ["id", "name", "created", "updated", "opt_in_process", "member_count"],
        "name,created,updated,opt_in_process", _since(since), row,
    )


@segments_app.command("export")
def segments_export(instance: str = INSTANCE) -> None:
    """Export every segment with its event labels (segments.csv) and members (segment_members.csv)."""
    known: dict = {}

    def row(client, group, count):
        if not known:
            known.update(segments.metrics(client))
        a = group["attributes"]
        return {"id": group["id"], "name": a["name"], "created": iso_or_none(a.get("created")),
                "updated": iso_or_none(a.get("updated")), "member_count": count,
                **segments.labels(a.get("definition"), known)}

    _export_groups(instance, "segment", segments.COLUMNS, "name,created,updated,definition", None, row)


@bis_app.command("export")
def bis_export(
    instance: str = INSTANCE,
    since: str | None = typer.Option(None, "--since", help="Only signups after this date or UTC time, e.g. 2026-09-01."),
) -> None:
    """Export Back in Stock signups as a STOQ import file (bis.csv), plus reference and excluded files."""
    since_dt = _since(since)
    run = new_run(instance, "bis")
    paths = {k: run.path(f".{k}.csv") for k in ("bis", "reference", "excluded")}
    writers = {
        "bis": CsvWriter(paths["bis"], bis.STOQ_COLUMNS),
        "reference": CsvWriter(paths["reference"], bis.REFERENCE_COLUMNS),
        "excluded": CsvWriter(paths["excluded"], bis.EXCLUDED_COLUMNS),
    }
    with _client(instance) as client:
        try:
            metric = bis.metric_id(client)
        except LookupError as exc:
            raise ConfigError(str(exc)) from exc
        counts = bis.build(
            bis.events(client, metric, since_dt),
            write=writers["bis"].write, reference=writers["reference"].write, exclude=writers["excluded"].write,
        )
    for w in writers.values():
        w.close()
    write_manifest(
        run, files={p.name: writers[k].count for k, p in paths.items()}, counts=counts,
        extra={"params": {"since": iso(since_dt)}} if since_dt else None,
    )
    typer.echo(f"{counts['events']:,} signups read: {counts['exported']:,} exported, {counts['excluded']:,} excluded")
    for k in ("bis", "reference", "excluded"):
        typer.echo(f"  {paths[k]}")


def _write_run(
    to: str, obj: str, main_step: str, file: Path, limit: int | None, yes: bool,
    allow_write_to_source: bool, body, extra_manifest: dict | None = None,
) -> None:
    """Shared frame for the write commands: read and check the file, confirm the
    target, run `body(importer, rows, columns, run)`, then write the skipped
    file, manifest and summary. Exits non-zero if anything failed."""
    inst = get_instance(to, "klaviyo")
    try:
        columns, raw = imports.read_rows(file, limit=limit)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    batch = imports.usable(raw)
    run = new_run(inst.name, obj)
    store = StateStore()
    with KlaviyoClient(credential(inst)) as client:
        account = client.account()
        typer.echo(f"File:            {file} ({len(raw):,} rows, {len(batch.skipped):,} skipped)")
        confirm_write(
            inst, account=f"{account['name']} ({account['id']})", record_count=len(batch.rows),
            yes=yes, allow_write_to_source=allow_write_to_source,
        )
        log = RunLog(run)
        log.read(len(raw))
        log.skipped(len(batch.skipped))

        def save_job(job: Job) -> None:
            store.add_job(inst.name, {"id": job.id, "kind": job.kind, "run_id": run.run_id, "size": job.size})

        imp = imports.Importer(Writer(client), log, echo=typer.echo, save_job=save_job, main_step=main_step)
        counts = body(imp, batch.rows, columns, run) or {}
        for job in imp.unfinished:
            log.error(f"job {job.id}", f"{job.kind} still {job.status or 'processing'}; check it later",
                      stage="wait")
    skipped_path = imports.write_skipped(run, batch.skipped)
    files = {skipped_path.name: len(batch.skipped)} if skipped_path else {}
    write_manifest(
        run, files=files, counts={**log.counts, **counts, "steps": imp.steps},
        status="complete" if not log.counts["failed"] else "completed with errors",
        extra={"migration_run_id": run.run_id, "source_file": str(file), "account": account["id"],
               **(extra_manifest or {})},
    )
    typer.echo(f"migration_run_id: {run.run_id}")
    if skipped_path:
        typer.echo(f"Skipped rows written to {skipped_path}")
    code = log.finish()
    if code:
        raise typer.Exit(code)


@profiles_app.command("import")
def profiles_import(
    to: str = TO,
    file: Path = FILE,
    list_id: str = typer.Option(..., "--list-id", help="List every imported profile joins and subscribes to."),
    limit: int | None = LIMIT,
    as_unsubscribe: bool = AS_UNSUBSCRIBE,
    yes: bool = YES,
    allow_write_to_source: bool = ALLOW_SOURCE,
) -> None:
    """Import profiles from a `profiles export` CSV, keeping consent and suppression."""

    def body(imp, rows, columns, run):
        imports.profiles_import(imp, rows, list_id=list_id, run_id=run.run_id, as_unsubscribe=as_unsubscribe)

    _write_run(to, "profiles-import", "import", file, limit, yes, allow_write_to_source, body,
               {"list_id": list_id, "as_unsubscribe": as_unsubscribe})


@suppressions_app.command("import")
def suppressions_import(
    to: str = TO,
    file: Path = FILE,
    limit: int | None = LIMIT,
    as_unsubscribe: bool = AS_UNSUBSCRIBE,
    yes: bool = YES,
    allow_write_to_source: bool = ALLOW_SOURCE,
) -> None:
    """Suppress every email in the file, creating suppressed profiles where none exist."""

    def body(imp, rows, columns, run):
        return {"created": imports.suppressions_import(imp, rows, run_id=run.run_id, as_unsubscribe=as_unsubscribe)}

    _write_run(to, "suppressions-import", "suppress", file, limit, yes, allow_write_to_source, body,
               {"as_unsubscribe": as_unsubscribe})


@suppressions_app.command("check")
def suppressions_check(
    instance: str = typer.Option(..., "--instance", help="Klaviyo instance to check."),
    file: Path = typer.Option(..., "--file", exists=True, dir_okay=False, help="CSV with an email column."),
) -> None:
    """Report whether each email in the file is suppressed now (read-only).

    Klaviyo can take hours to apply suppression jobs, so run this some time
    after `suppressions import`. Emails not yet suppressed go to a CSV."""
    try:
        _, raw = imports.read_rows(file)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    batch = imports.usable(raw)
    with _client(instance) as client:
        states = suppressions.check(client, (r["email"] for r in batch.rows))
    counts: dict[str, int] = {}
    for row in states.values():
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    run = new_run(instance, "suppressions-check")
    pending = [r for r in states.values() if r["state"] != "suppressed"]
    files = {}
    if pending:
        path = run.path(".not_suppressed.csv")
        writer = CsvWriter(path, suppressions.CHECK_COLUMNS)
        for row in pending:
            writer.write(row)
        writer.close()
        files[path.name] = writer.count
    write_manifest(run, files=files, counts=counts, extra={"source_file": str(file)})
    for state in ("suppressed", "unsubscribed", "not suppressed", "no profile"):
        typer.echo(f"{state:<15} {counts.get(state, 0):,}")
    if pending:
        typer.echo(f"Not yet suppressed: {run.path('.not_suppressed.csv')}")


@lists_app.command("add")
def lists_add(
    to: str = TO,
    list_id: str = typer.Option(..., "--list", help="ID of the list to add profiles to."),
    file: Path = FILE,
    limit: int | None = LIMIT,
    yes: bool = YES,
    allow_write_to_source: bool = ALLOW_SOURCE,
) -> None:
    """Add every profile in the file to one list; writes consent only if the file has consent columns."""

    def body(imp, rows, columns, run):
        return {"created": imports.lists_add(imp, rows, columns, list_id=list_id, run_id=run.run_id)}

    _write_run(to, "lists-add", "add", file, limit, yes, allow_write_to_source, body, {"list_id": list_id})


def main() -> None:
    """Console entry point: turns expected errors into a message and an exit code."""
    try:
        app()
    except (ConfigError, WriteRefused, ResumeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        sys.exit(2)
    except ApiError as exc:
        typer.echo(f"API error: {exc}", err=True)
        sys.exit(1)
