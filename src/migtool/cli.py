"""`migtool` command line. Service commands are added phase by phase."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import typer

from migtool.config import (
    INSTANCES, ConfigError, check_account, check_shop, credential, get_instance, load_env, shopify_shop,
)
from migtool.http import ApiError
from migtool.klaviyo import bis, dedupe, groups, imports, profiles, properties, segments, suppressions
from migtool.klaviyo.client import KlaviyoClient
from migtool.klaviyo.writes import Job, Writer
from migtool.output import (
    CsvWriter,
    utc_now,
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
from migtool.shopify.client import ShopifyError
from migtool.state import StateStore

# Locals are hidden in tracebacks so a crash can't print a key.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
klaviyo_app = typer.Typer(no_args_is_help=True, help="Klaviyo exports and imports.")
app.add_typer(klaviyo_app, name="klaviyo")
shopify_app = typer.Typer(no_args_is_help=True, help="Shopify: customers and their marketing consent. Read-only except `consent-sync` (email consent).")
app.add_typer(shopify_app, name="shopify")
profiles_app = typer.Typer(no_args_is_help=True, help="Profiles.")
lists_app = typer.Typer(no_args_is_help=True, help="Lists and their members.")
segments_app = typer.Typer(no_args_is_help=True, help="Segments and their members.")
suppressions_app = typer.Typer(no_args_is_help=True, help="Email suppressions.")
bis_app = typer.Typer(no_args_is_help=True, help="Back in Stock signups, for upload to STOQ.")
dedupe_app = typer.Typer(no_args_is_help=True, help="Import the dedupe files (Klaviyo UI-import layout).")
klaviyo_app.add_typer(profiles_app, name="profiles")
klaviyo_app.add_typer(lists_app, name="lists")
klaviyo_app.add_typer(segments_app, name="segments")
klaviyo_app.add_typer(suppressions_app, name="suppressions")
klaviyo_app.add_typer(bis_app, name="bis")
klaviyo_app.add_typer(dedupe_app, name="dedupe")
events_app = typer.Typer(no_args_is_help=True, help="Custom events (re-send a flow's trigger).")
klaviyo_app.add_typer(events_app, name="events")

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
    check_account(inst, acct["id"])


def _since(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_iso(value)
    except ValueError as exc:
        raise typer.BadParameter(f"'{value}' is not an ISO 8601 time.", param_hint="--since") from exc


def _connect(inst) -> tuple[KlaviyoClient, dict]:
    """A client for `inst`, after checking its key belongs to the expected account.
    Writes retried after an ambiguous failure are recorded in state/."""
    store = StateStore()

    def warn(message: str) -> None:
        typer.echo(message, err=True)
        store.add_ambiguous_write(inst.name, {"time": iso(utc_now()), "message": message})

    client = KlaviyoClient(credential(inst), warn=warn)
    try:
        account = client.account()
        check_account(inst, account["id"])
    except BaseException:
        client.close()
        raise
    return client, account


RETRIES_SETTLED = typer.Option(
    False, "--retries-settled",
    help="Confirm that writes retried after a lost response (recorded in state/) have settled, then continue.",
)


def _warn_unsettled(inst) -> None:
    entries = StateStore().ambiguous_writes(inst.name)
    if entries:
        typer.echo(f"Note: {len(entries)} write(s) to {inst.name} were retried after a lost response "
                   f"(state/{inst.name}/ambiguous_writes.json); Klaviyo may still be applying a first copy.")


def _gate_unsettled(inst, retries_settled: bool) -> None:
    """Stop a write while earlier ambiguous retries are unconfirmed: a delayed
    first copy could land after this write and undo it (say, re-set a hold
    after 05 released it). `--retries-settled` confirms and clears them."""
    store = StateStore()
    entries = store.ambiguous_writes(inst.name)
    if not entries:
        return
    if not retries_settled:
        raise ConfigError(
            f"{len(entries)} earlier write(s) to {inst.name} were retried after a lost response "
            f"(state/{inst.name}/ambiguous_writes.json), so a first copy may still land. Wait a few minutes, "
            "run `klaviyo dedupe check` on the affected file, then re-run this with --retries-settled."
        )
    for e in entries:
        typer.echo(f"Settled: {e['time']} {e['message'][:120]}")
    store.clear_ambiguous_writes(inst.name)


JOB_DONE = ("complete", "cancelled", "failed")
# Klaviyo drops jobs after seven days: "not found" is recorded so it isn't
# looked up again, and kept distinct from a confirmed status.
JOB_SETTLED = (*JOB_DONE, "not found")


def _gate_pending_jobs(client: KlaviyoClient, inst) -> None:
    """Stop a write while a profile import job an earlier run submitted is
    still processing (after Ctrl-C, a polling failure or a timeout): Klaviyo
    doesn't guarantee order, so it could land after this write and undo it.
    Saved jobs are looked up once and their final status recorded."""
    store = StateStore()
    open_jobs = [j for j in store.jobs(inst.name)
                 if j.get("kind") == "profile-bulk-import-jobs" and j.get("status") not in JOB_SETTLED]
    if not open_jobs:
        return
    updates: dict[str, dict] = {}
    pending = []
    for job in open_jobs:
        try:
            status = client.get(f"/{job['kind']}/{job['id']}/", tier="L")["data"]["attributes"]["status"]
        except ApiError as exc:
            if exc.status != 404:
                raise
            status = "not found"  # Klaviyo no longer has it: long finished.
        if status in JOB_DONE or status == "not found":
            updates[job["id"]] = {"status": status}
        else:
            pending.append((job, status))
    if updates:
        store.update_jobs(inst.name, updates)
    if pending:
        runs = sorted({j.get("run_id", "?") for j, _ in pending})
        raise ConfigError(
            f"{len(pending)} earlier profile import job(s) on {inst.name} are still processing "
            f"(runs {', '.join(runs)}; state/{inst.name}/jobs.json). Wait for them to finish, then re-run."
        )


def _client(instance: str) -> KlaviyoClient:
    return _connect(get_instance(instance, "klaviyo"))[0]


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
            typed_prefix="properties.",
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
            instance, "suppressions", suppressions.COLUMNS, resume=resume, staged=True,
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
    allow_write_to_source: bool, body, extra_manifest: dict | None = None, retries_settled: bool = False,
    phones: bool = False, preview=None,
) -> None:
    """Shared frame for the write commands: read and check the file, confirm the
    target, run `body(importer, rows, columns, run)`, then write the skipped
    file, manifest and summary. Exits non-zero if anything failed.

    `preview(client, rows)` (optional, read-only) runs before the confirmation
    and returns (rows to send, skipped rows), so the count confirmed is what
    will be written."""
    inst = get_instance(to, "klaviyo")
    _gate_unsettled(inst, retries_settled)
    try:
        columns, raw = imports.read_rows(file, limit=limit)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    batch = imports.usable(raw, phones=phones)
    run = new_run(inst.name, obj)
    store = StateStore()
    client, account = _connect(inst)
    with client:
        _gate_pending_jobs(client, inst)
        rows, previewed = preview(client, batch.rows) if preview else (batch.rows, [])
        typer.echo(f"File:            {file} ({len(raw):,} rows, {len(batch.skipped) + len(previewed):,} skipped)")
        confirm_write(
            inst, account=f"{account['name']} ({account['id']})", record_count=len(rows),
            yes=yes, allow_write_to_source=allow_write_to_source,
        )
        log = RunLog(run, written_label="submitted" if main_step == "suppress" else "written")
        log.read(len(raw))
        log.skipped(len(batch.skipped) + len(previewed))

        def save_job(job: Job) -> None:
            store.add_job(inst.name, {"id": job.id, "kind": job.kind, "run_id": run.run_id, "size": job.size})

        imp = imports.Importer(Writer(client), log, echo=typer.echo, save_job=save_job, main_step=main_step)
        counts: dict = {}
        aborted: str | None = None
        try:
            counts = body(imp, rows, columns, run) or {}
        except (Exception, KeyboardInterrupt) as exc:
            # Stop, but still record what was sent: earlier jobs may be running.
            aborted = "interrupted" if isinstance(exc, KeyboardInterrupt) else f"{type(exc).__name__}: {exc}"
            log.error("run", f"stopped: {aborted}. Jobs already submitted are in state/ and may still "
                      "be applied; re-running the file is safe.", stage="aborted")
        for job in imp.unfinished:
            typer.echo(f"Job {job.id} ({job.kind}) was still {job.status or 'processing'} when the run ended.")
    skipped = batch.skipped + previewed + imp.skipped
    skipped_path = imports.write_skipped(run, skipped)
    files = {skipped_path.name: len(skipped)} if skipped_path else {}
    if aborted:
        status = "aborted"
    else:
        status = "complete" if not log.counts["failed"] else "completed with errors"
    write_manifest(
        run, files=files, counts={**log.counts, **counts, "steps": imp.steps}, status=status,
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
    retries_settled: bool = RETRIES_SETTLED,
) -> None:
    """Import profiles from a `profiles export` CSV, keeping consent and suppression."""

    def body(imp, rows, columns, run):
        imports.profiles_import(imp, rows, list_id=list_id, run_id=run.run_id, as_unsubscribe=as_unsubscribe)

    _write_run(to, "profiles-import", "import", file, limit, yes, allow_write_to_source, body,
               {"list_id": list_id, "as_unsubscribe": as_unsubscribe}, retries_settled=retries_settled)


def _property(key: str, value: str, kind: str):
    try:
        return properties.check_key(key), properties.parse_value(value, kind)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


KEY = typer.Option(..., "--key", help="Custom property name, e.g. catchup_hold.")
VALUE = typer.Option(..., "--value", help="Value to set, e.g. true.")
KIND = typer.Option("bool", "--type", help="Value type: " + ", ".join(properties.KINDS) + ".")


@profiles_app.command("set-property")
def profiles_set_property(
    to: str = TO,
    file: Path = FILE,
    key: str = KEY,
    value: str = VALUE,
    kind: str = KIND,
    limit: int | None = LIMIT,
    yes: bool = YES,
    allow_write_to_source: bool = ALLOW_SOURCE,
    retries_settled: bool = RETRIES_SETTLED,
) -> None:
    """Set one custom property on the existing profiles in a CSV (`email` column).

    Only that property is sent: consent, lists and other fields are untouched.
    Emails without a profile are skipped (not created) and listed in the
    skipped file; the lookup runs just before the write, which is an upsert.
    Run `profiles check-property` afterwards."""
    key, parsed = _property(key, value, kind)

    def preview(client, rows):
        w = Writer(client)
        keep, missing = properties.plan(rows, w.existing_emails)
        typer.echo(f"Property:        {key} = {parsed!r} ({kind})")
        typer.echo(f"To update:       {len(keep):,} existing profiles; {len(missing):,} emails have no profile")
        return keep, missing

    def body(imp, rows, columns, run):
        imp.import_profiles(properties.payloads(rows, key, parsed), list_id=None, stage="update")

    _write_run(to, "set-property", "update", file, limit, yes, allow_write_to_source, body,
               {"key": key, "value": parsed, "type": kind}, retries_settled=retries_settled, preview=preview)


@profiles_app.command("check-property")
def profiles_check_property(
    instance: str = typer.Option(..., "--instance", help="Klaviyo instance to check."),
    file: Path = FILE,
    key: str = KEY,
    value: str = VALUE,
    kind: str = KIND,
    limit: int | None = LIMIT,
) -> None:
    """Check (read-only) that every email in the CSV has a profile with the
    property set to the value (type for type). Problems go to
    <run>.mismatches.csv, rows that can't be checked to <run>.skipped.csv;
    either, or nothing checked, exits 1. Klaviyo can take a few minutes to
    apply an import."""
    key, parsed = _property(key, value, kind)
    inst = get_instance(instance, "klaviyo")
    try:
        _, raw = imports.read_rows(file, limit=limit)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    batch = imports.usable(raw)
    _warn_unsettled(inst)
    client, _ = _connect(inst)
    with client:
        counts, mismatches = properties.check(client, [r["email"] for r in batch.rows], key, parsed,
                                              progress=lambda m: typer.echo(f"  {m}"))
    # A repeated email is checked once; any other skipped row couldn't be
    # checked at all, so the file isn't fully verified.
    unchecked = [(who, why) for who, why in batch.skipped if not why.startswith("duplicate")]
    run = new_run(inst.name, "check-property")
    files = {}
    if mismatches:
        path = run.path(".mismatches.csv")
        w = CsvWriter(path, properties.CHECK_COLUMNS)
        for m in mismatches:
            w.write(m)
        w.close()
        files[path.name] = w.count
    skipped_path = imports.write_skipped(run, batch.skipped)
    if skipped_path:
        files[skipped_path.name] = len(batch.skipped)
    write_manifest(run, files=files, counts={**counts, "skipped": len(batch.skipped), "unchecked": len(unchecked)},
                   extra={"source_file": str(file), "key": key, "value": parsed, "type": kind})
    typer.echo(f"checked {counts['checked']:,}: ok {counts['ok']:,}, no profile {counts['no profile']:,}, "
               f"mismatched {counts['mismatched']:,}; {len(unchecked):,} rows couldn't be checked, "
               f"{len(batch.skipped) - len(unchecked):,} duplicates")
    if mismatches:
        typer.echo(f"Mismatches: {run.path('.mismatches.csv')}")
    if skipped_path:
        typer.echo(f"Skipped rows: {skipped_path}")
    if mismatches or unchecked or not counts["checked"]:
        if not counts["checked"]:
            typer.echo("Nothing was checked: no usable email in the file.")
        raise typer.Exit(1)


@suppressions_app.command("import")
def suppressions_import(
    to: str = TO,
    file: Path = FILE,
    limit: int | None = LIMIT,
    as_unsubscribe: bool = AS_UNSUBSCRIBE,
    yes: bool = YES,
    allow_write_to_source: bool = ALLOW_SOURCE,
    retries_settled: bool = RETRIES_SETTLED,
) -> None:
    """Suppress every email in the file, creating suppressed profiles where none exist."""

    def body(imp, rows, columns, run):
        return {"created": imports.suppressions_import(imp, rows, run_id=run.run_id, as_unsubscribe=as_unsubscribe)}

    _write_run(to, "suppressions-import", "suppress", file, limit, yes, allow_write_to_source, body,
               {"as_unsubscribe": as_unsubscribe}, retries_settled=retries_settled)


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
    retries_settled: bool = RETRIES_SETTLED,
    existing_only: bool = typer.Option(
        False, "--existing-only", help="Only add profiles that already exist at the destination; skip the rest."
    ),
    source: str = typer.Option("ca", "--source", help="The migrated_from tag for profiles this creates."),
) -> None:
    """Add every profile in the file to one list, by email or (phone-only rows) phone number; writes
    consent only if the file has consent columns."""

    def body(imp, rows, columns, run):
        return {"created": imports.lists_add(imp, rows, columns, list_id=list_id, run_id=run.run_id,
                                             existing_only=existing_only, source=source)}

    _write_run(to, "lists-add", "add", file, limit, yes, allow_write_to_source, body, {"list_id": list_id},
               retries_settled=retries_settled, phones=True)



TO_LIST = typer.Option(None, "--to-list", help="ID of an existing list to add the members to.")
CREATE = typer.Option(False, "--create", help="Create the destination list instead.")
EXISTING_ONLY = typer.Option(
    False, "--existing-only", help="Only add profiles that already exist at the destination; skip the rest."
)
NAME = typer.Option(None, "--name", help="Name of the list --create makes, used as is (default: the source's name plus --suffix).")


def _provenance(instance: str) -> str:
    """The `migrated_from` tag for profiles a copy from `instance` creates."""
    return "ca" if instance == "klaviyo_ca" else instance.removeprefix("klaviyo_")


def _copy_group(
    kind: str, from_: str, source_id: str, to: str, to_list: str | None, create: bool, name: str | None,
    suffix: str, limit: int | None, yes: bool, allow_write_to_source: bool, retries_settled: bool,
    existing_only: bool = False,
) -> None:
    """Shared body of `lists copy` and `segments copy`: snapshot one list's or
    segment's members (read-only), then add them to a destination list."""
    if (to_list is None) != create:
        raise typer.BadParameter("give exactly one of --to-list or --create.", param_hint="--to-list")
    if name and not create:
        raise typer.BadParameter("--name only applies with --create.", param_hint="--name")
    label = kind.capitalize()
    source = new_run(from_, f"{kind}s-copy")
    members_path = source.path(".members.csv")
    with _client(from_) as client:
        try:
            group = client.get(f"/{kind}s/{source_id}/", tier="S", params={f"fields[{kind}]": "name"})["data"]
        except ApiError as exc:
            raise ConfigError(f"{label} {source_id} wasn't found on {from_} ({exc.status}).") from exc
        source_name = group["attributes"]["name"]
        # The phone only for phone-only members: it's their identifier. For the
        # others, sending it could change the destination profile's number.
        writer = CsvWriter(members_path, ["email", "phone_number"])
        phone_only = neither = 0
        params = {"fields[profile]": "email,phone_number", "page[size]": "100"}
        for page in client.paginate(f"/{kind}s/{source_id}/profiles/", tier="L", params=params):
            for p in page["data"]:
                email, phone = p["attributes"].get("email"), p["attributes"].get("phone_number")
                if email:
                    writer.write({"email": email, "phone_number": ""})
                elif phone:
                    writer.write({"email": "", "phone_number": phone})
                    phone_only += 1
                else:
                    neither += 1
        writer.close()
    write_manifest(source, files={members_path.name: writer.count},
                   counts={"members": writer.count + neither, "phone_only": phone_only, "no_identifier": neither},
                   extra={f"{kind}_id": source_id, f"{kind}_name": source_name})
    typer.echo(f"Source {kind + ':':10}{source_name} ({source_id}) on {from_}: {writer.count + neither:,} members "
               f"({phone_only:,} phone-only), {neither:,} with no email or phone (skipped)")
    with _client(to) as client:
        if to_list:
            dest_name = _list_name(client, to_list)
            typer.echo(f"Add to:          {dest_name} ({to_list})")
        else:
            dest_name = name or f"{source_name}{suffix}"
            if groups.lists_named(client, dest_name):
                raise ConfigError(f"{to} already has a list named '{dest_name}'. Use --to-list with its ID, "
                                  "or --name for a different name.")
            typer.echo(f"Create list:     {dest_name} (after you confirm)")
    extra = {"source_instance": from_, f"source_{kind}": source_id, "source_members_file": str(members_path),
             "list_id": to_list, "list_name": dest_name}

    def body(imp, rows, columns, run):
        list_id = to_list
        if not list_id:
            list_id = imp.w.create_list(dest_name)
            extra["list_id"] = list_id
            typer.echo(f"Created list {dest_name} ({list_id})")
        return {"created": imports.lists_add(imp, rows, columns, list_id=list_id, run_id=run.run_id,
                                             source=_provenance(from_), existing_only=existing_only)}

    try:
        _write_run(to, f"{kind}s-copy", "add", members_path, limit, yes, allow_write_to_source, body, extra,
                   retries_settled=retries_settled, phones=True)
    except typer.Exit:
        # Re-running would take a new snapshot and miss anyone who has left the
        # source since. Finish this one from its saved file instead.
        if extra["list_id"]:
            options = (f" --limit {limit}" if limit else "") + (" --existing-only" if existing_only else "")
            if _provenance(from_) != "ca":
                options += f" --source {_provenance(from_)}"
            typer.echo(f"To finish this copy from the same snapshot: uv run migtool klaviyo lists add --to {to} "
                       f"--list {extra['list_id']} --file {members_path}{options}")
        raise


@lists_app.command("copy")
def lists_copy(
    from_: str = typer.Option(..., "--from", help="Klaviyo instance to read the list from (read-only)."),
    source_list: str = typer.Option(..., "--list", help="ID of the list to copy."),
    to: str = TO,
    to_list: str | None = TO_LIST,
    create: bool = CREATE,
    name: str | None = NAME,
    suffix: str = typer.Option(" (CA)", "--suffix", help="Added to the source list's name when --name isn't given."),
    limit: int | None = LIMIT,
    yes: bool = YES,
    allow_write_to_source: bool = ALLOW_SOURCE,
    retries_settled: bool = RETRIES_SETTLED,
    existing_only: bool = EXISTING_ONLY,
) -> None:
    """Copy one list's members into a list on another instance, creating it if asked.

    Members are matched by email, or by phone number when they have no email,
    and only added to the list: no field, property or consent changes. A member
    with no profile at the destination is created, with the migration tags."""
    _copy_group("list", from_, source_list, to, to_list, create, name, suffix, limit, yes,
                allow_write_to_source, retries_settled, existing_only)


@segments_app.command("copy")
def segments_copy(
    from_: str = typer.Option(..., "--from", help="Klaviyo instance to read the segment from (read-only)."),
    source_segment: str = typer.Option(..., "--segment", help="ID of the segment to copy."),
    to: str = TO,
    to_list: str | None = TO_LIST,
    create: bool = CREATE,
    name: str | None = NAME,
    suffix: str = typer.Option(
        " (CA segment)", "--suffix", help="Added to the segment's name when --name isn't given."
    ),
    limit: int | None = LIMIT,
    yes: bool = YES,
    allow_write_to_source: bool = ALLOW_SOURCE,
    retries_settled: bool = RETRIES_SETTLED,
    existing_only: bool = EXISTING_ONLY,
) -> None:
    """Copy a segment's current members into a static list on another instance.

    A snapshot: the list doesn't follow the segment afterwards. Members are
    matched and added as `lists copy` does."""
    _copy_group("segment", from_, source_segment, to, to_list, create, name, suffix, limit, yes,
                allow_write_to_source, retries_settled, existing_only)


TYPES_FROM = typer.Option(
    None, "--types-from", exists=True, dir_okay=False,
    help="The CA `profiles export` CSV, whose header gives each custom property's type (required for role new).",
)
US_SNAPSHOT = typer.Option(
    None, "--us-snapshot", exists=True, dir_okay=False,
    help="The pre-migration US `profiles export` CSV: which 02 rows are existing US profiles (required for role suppress).",
)


def _check_role_options(r, join_list, subscribe_list, types_from, us_snapshot) -> None:
    """The option rules shared by `dedupe import` and `dedupe check`."""
    if r.join_list and not join_list:
        raise typer.BadParameter(f"role {r.name} needs --join-list.", param_hint="--join-list")
    if not r.join_list and join_list:
        raise typer.BadParameter(f"role {r.name} doesn't join a list.", param_hint="--join-list")
    if subscribe_list and not r.consent:
        raise typer.BadParameter(f"role {r.name} doesn't subscribe anyone.", param_hint="--subscribe-list")
    if r.name == "new" and not types_from:
        raise typer.BadParameter("role new needs --types-from (the CA profile export).", param_hint="--types-from")
    if r.name == "suppress" and not us_snapshot:
        raise typer.BadParameter("role suppress needs --us-snapshot (the pre-migration US profile export).",
                                 param_hint="--us-snapshot")


def _list_name(client: KlaviyoClient, list_id: str) -> str:
    try:
        return client.get(f"/lists/{list_id}/", tier="S", params={"fields[list]": "name"})["data"]["attributes"]["name"]
    except ApiError as exc:
        raise ConfigError(f"List {list_id} wasn't found in this account ({exc.status}).") from exc


@dedupe_app.command("import")
def dedupe_import(
    to: str = TO,
    file: Path = FILE,
    role: str = typer.Option(
        ..., "--role",
        help="What the file is: " + "; ".join(f"{r.name} ({r.files})" for r in dedupe.ROLES.values()) + ".",
    ),
    join_list: str | None = typer.Option(None, "--join-list", help="List the profiles join (roles suppress, new, kept)."),
    subscribe_list: str | None = typer.Option(
        None, "--subscribe-list", help="List Subscribe rows subscribe to, as a historical import (roles new, kept)."
    ),
    types_from: Path | None = TYPES_FROM,
    us_snapshot: Path | None = US_SNAPSHOT,
    limit: int | None = LIMIT,
    yes: bool = YES,
    allow_write_to_source: bool = ALLOW_SOURCE,
    retries_settled: bool = RETRIES_SETTLED,
) -> None:
    """Import one dedupe file under the rules of its role (see docs/DEDUPE_IMPORT.md)."""
    if role not in dedupe.ROLES:
        raise typer.BadParameter(f"'{role}' isn't one of {', '.join(dedupe.ROLES)}.", param_hint="--role")
    r = dedupe.ROLES[role]
    _check_role_options(r, join_list, subscribe_list, types_from, us_snapshot)
    inst = get_instance(to, "klaviyo")
    _gate_unsettled(inst, retries_settled)
    try:
        columns, raw = imports.read_rows(file, limit=limit)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    types = dedupe.type_map(types_from) if types_from else {}
    snapshot = dedupe.load_snapshot(us_snapshot) if us_snapshot else None
    run = new_run(inst.name, f"dedupe-{role}")
    store = StateStore()
    client, account = _connect(inst)
    with client:
        _gate_pending_jobs(client, inst)
        w = Writer(client)
        p = dedupe.plan(r, raw, types, run.run_id, lambda e, ph: w.existing_emails(e) | w.existing_phones(ph),
                        us_snapshot=snapshot)
        if r.consent and p.count("SUBSCRIBED") and not subscribe_list:
            raise typer.BadParameter(
                f"{p.count('SUBSCRIBED'):,} rows say Subscribe; give --subscribe-list.", param_hint="--subscribe-list"
            )
        typer.echo(f"File:            {file} ({len(raw):,} rows)")
        typer.echo(f"Role:            {role} ({'creates and updates' if r.creates else 'updates existing profiles only'})")
        typer.echo(f"To send:         {len(p.rows):,} ({p.updates:,} existing, {p.creates:,} new)")
        typer.echo(f"Not sent:        {len(p.skipped):,} skipped, {len(p.unreadable):,} unreadable")
        if join_list:
            typer.echo(f"Join list:       {join_list} ({_list_name(client, join_list)})"
                       + (" (existing profiles only)" if role == "suppress" else ""))
        if r.consent:
            if subscribe_list:
                typer.echo(f"Subscribe to:    {subscribe_list} ({_list_name(client, subscribe_list)}), "
                           f"{p.count('SUBSCRIBED'):,} rows")
            typer.echo(f"Unsubscribe:     {p.count('UNSUBSCRIBED'):,} rows")
        if role == "suppress":
            typer.echo(f"Suppress:        {sum(1 for x in p.rows if x['email']):,} emails")
        typer.echo(f"Migration tags:  {'yes' if r.tags else 'no'}")
        confirm_write(
            inst, account=f"{account['name']} ({account['id']})", record_count=len(p.rows),
            yes=yes, allow_write_to_source=allow_write_to_source,
        )
        main_step = {"hold": "update", "kept": "update", "consent": "update", "suppress": "suppress"}.get(role, "import")
        log = RunLog(run, written_label="submitted" if main_step == "suppress" else "written")
        log.read(len(raw))
        log.skipped(len(p.skipped))
        for who, reason in p.unreadable:
            log.error(who, reason, stage="read")

        def save_job(job: Job) -> None:
            store.add_job(inst.name, {"id": job.id, "kind": job.kind, "run_id": run.run_id, "size": job.size})

        imp = imports.Importer(w, log, echo=typer.echo, save_job=save_job, main_step=main_step)
        aborted: str | None = None
        try:
            dedupe.run(imp, p, join_list=join_list, subscribe_list=subscribe_list)
        except (Exception, KeyboardInterrupt) as exc:
            aborted = "interrupted" if isinstance(exc, KeyboardInterrupt) else f"{type(exc).__name__}: {exc}"
            log.error("run", f"stopped: {aborted}. Jobs already submitted are in state/ and may still "
                      "be applied; re-running the file is safe.", stage="aborted")
        for job in imp.unfinished:
            typer.echo(f"Job {job.id} ({job.kind}) was still {job.status or 'processing'} when the run ended.")
    skipped_path = imports.write_skipped(run, p.skipped)
    status = "aborted" if aborted else ("complete" if not log.counts["failed"] else "completed with errors")
    write_manifest(
        run, files={skipped_path.name: len(p.skipped)} if skipped_path else {},
        counts={**log.counts, "steps": imp.steps}, status=status,
        extra={"migration_run_id": run.run_id, "source_file": str(file), "account": account["id"], "role": role,
               "join_list": join_list, "subscribe_list": subscribe_list,
               "types_from": str(types_from) if types_from else None,
               "us_snapshot": str(us_snapshot) if us_snapshot else None},
    )
    typer.echo(f"migration_run_id: {run.run_id}")
    if skipped_path:
        typer.echo(f"Skipped rows written to {skipped_path}")
    code = log.finish()
    if code:
        raise typer.Exit(code)


@dedupe_app.command("check")
def dedupe_check(
    instance: str = typer.Option(..., "--instance", help="Klaviyo instance to check."),
    file: Path = FILE,
    role: str = typer.Option(..., "--role", help="The role the file was imported with."),
    join_list: str | None = typer.Option(None, "--join-list", help="List the profiles should be on (as imported)."),
    subscribe_list: str | None = typer.Option(
        None, "--subscribe-list", help="List Subscribe rows should be on (as imported)."
    ),
    types_from: Path | None = TYPES_FROM,
    us_snapshot: Path | None = US_SNAPSHOT,
    limit: int | None = LIMIT,
) -> None:
    """Check (read-only) that each row of a dedupe file landed as its role intends.

    For every row: the profile exists; every field and property the role
    writes matches (with its type); migration tags; list membership (by
    profile ID); consent, and a subscribe date no later than the file's;
    suppression where the row carries one, and none on subscribed rows. Rows
    that don't match go to <run>.mismatches.csv. Takes the same options as the
    import, and needs them."""
    if role not in dedupe.ROLES:
        raise typer.BadParameter(f"'{role}' isn't one of {', '.join(dedupe.ROLES)}.", param_hint="--role")
    r = dedupe.ROLES[role]
    _check_role_options(r, join_list, subscribe_list, types_from, us_snapshot)
    inst = get_instance(instance, "klaviyo")
    try:
        _, raw = imports.read_rows(file, limit=limit)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if r.consent and not subscribe_list and any(dedupe.consent_instruction(x) == "SUBSCRIBED" for x in raw
                                                  if x.get(dedupe.CONSENT)):
        raise typer.BadParameter("the file has Subscribe rows; give --subscribe-list.", param_hint="--subscribe-list")
    types = dedupe.type_map(types_from) if types_from else {}
    snapshot = dedupe.load_snapshot(us_snapshot) if us_snapshot else None
    _warn_unsettled(inst)
    client, _ = _connect(inst)
    with client:
        counts, mismatches, skipped = dedupe.check(
            client, r, raw, types=types, us_snapshot=snapshot, join_list=join_list,
            subscribe_list=subscribe_list, progress=lambda m: typer.echo(f"  {m}"),
        )
    run = new_run(inst.name, f"dedupe-check-{role}")
    files = {}
    if mismatches:
        path = run.path(".mismatches.csv")
        w = CsvWriter(path, dedupe.CHECK_COLUMNS)
        for m in mismatches:
            w.write(m)
        w.close()
        files[path.name] = w.count
    write_manifest(run, files=files, counts={**counts, "skipped": len(skipped)},
                   extra={"source_file": str(file), "role": role, "join_list": join_list,
                          "subscribe_list": subscribe_list})
    typer.echo(f"checked {counts['checked']:,}: ok {counts['ok']:,}, mismatched {counts['mismatched']:,}"
               f" (skipped {len(skipped):,} rows the import also skips)")
    if mismatches:
        typer.echo(f"Mismatches: {run.path('.mismatches.csv')}")
        raise typer.Exit(1)




UNKNOWN_SEND = "unknown, may have been accepted"


@events_app.command("resend")
def events_resend(
    to: str = TO,
    file: Path = FILE,
    metric: str = typer.Option(..., "--metric", help="Name of the custom metric to send (the resend flow's trigger)."),
    send_to: str | None = typer.Option(None, "--send-to", help="Send every event to this email instead (for tests)."),
    limit: int | None = LIMIT,
    yes: bool = YES,
    allow_write_to_source: bool = ALLOW_SOURCE,
    retries_settled: bool = RETRIES_SETTLED,
) -> None:
    """Re-send orders' Shopify Placed Order data as a custom event, to trigger a resend flow.

    The file needs `email` (the customer) and `order_id` or `order_name`. Each
    event is an exact copy of the order's Placed Order properties, sent to the
    customer's profile (or `--send-to`). Repeats are dropped by Klaviyo."""
    from migtool.klaviyo import events

    inst = get_instance(to, "klaviyo")
    _gate_unsettled(inst, retries_settled)
    try:
        _, rows = imports.read_rows(file, limit=limit)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    run = new_run(inst.name, "events-resend")
    client, account = _connect(inst)
    with client:
        _gate_pending_jobs(client, inst)
        try:
            mid = events.source_metric_id(client)
        except LookupError as exc:
            raise ConfigError(str(exc)) from exc
        p = events.plan(client, rows, metric_id=mid, send_to=send_to)
        typer.echo(f"File:            {file} ({len(rows):,} rows)")
        typer.echo(f"Metric:          {metric}")
        typer.echo(f"Ready:           {len(p.ready):,}; not sendable: {len(p.items) - len(p.ready):,}")
        if send_to:
            typer.echo(f"Send to:         {send_to} (test: not the customers)")
        for it in p.items:
            matched = (f"order {events.order_name(it.event)} (id {events.order_id(it.event)}, placed "
                       f"{it.event['attributes'].get('datetime', '')[:19]})") if it.event else it.problem
            typer.echo(f"  {it.order:>16}  {it.customer:40} -> {it.recipient}  {matched}")
        confirm_write(inst, account=f"{account['name']} ({account['id']})", record_count=len(p.ready),
                      yes=yes, allow_write_to_source=allow_write_to_source)
        log = RunLog(run, written_label="submitted")
        log.read(len(rows))
        results = {id(it): ("not sent: " + it.problem) if it.problem else "not attempted" for it in p.items}
        for it in p.items:
            if it.problem:
                log.error(f"{it.order} ({it.customer or '?'})", it.problem, stage="plan")
        now = iso(utc_now())
        aborted = None
        try:
            for it in p.ready:
                # Until Klaviyo answers, the event may or may not have arrived.
                results[id(it)] = UNKNOWN_SEND
                try:
                    events.send(client, events.event_body(it, metric=metric, time=now))
                    results[id(it)] = "submitted"
                    log.written()
                except ApiError as exc:
                    if exc.status is not None and 400 <= exc.status < 500:
                        results[id(it)] = f"refused: {exc.detail}"
                        log.error(f"{it.order} ({it.customer})", f"refused by Klaviyo: {exc.detail}", stage="send")
                    else:
                        results[id(it)] = f"{UNKNOWN_SEND}: {exc.detail}"
                        log.error(f"{it.order} ({it.customer})", f"{UNKNOWN_SEND}: {exc.detail}", stage="send")
        except KeyboardInterrupt:
            aborted = "interrupted"
            log.error("run", "stopped: interrupted; rows marked 'not attempted' weren't sent, and a row marked "
                      f"'{UNKNOWN_SEND}' may have been. Re-sending is safe: the fixed unique_id stops repeats.",
                      stage="aborted")
        finally:
            path = run.path(".results.csv")
            writer = CsvWriter(path, ["order", "customer", "recipient", "metric", "source_event", "matched_order_id",
                                      "matched_order_name", "unique_id", "outcome"])
            for it in p.items:
                writer.write({
                    "order": it.order, "customer": it.customer, "recipient": it.recipient, "metric": metric,
                    "source_event": it.event["id"] if it.event else "",
                    "matched_order_id": events.order_id(it.event) if it.event else "",
                    "matched_order_name": events.order_name(it.event) if it.event else "",
                    "unique_id": (events.event_body(it, metric=metric, time=now)["data"]["attributes"]["unique_id"]
                                  if it.event else ""),
                    "outcome": results[id(it)]})
            writer.close()
            status = aborted or ("complete" if not log.counts["failed"] else "completed with errors")
            write_manifest(run, files={path.name: writer.count}, counts=dict(log.counts), status=status,
                           extra={"source_file": str(file), "metric": metric, "send_to": send_to,
                                  "account": account["id"]})
    typer.echo(f"Results per order: {path}")
    code = log.finish()
    if code or aborted:
        raise typer.Exit(code or 130)



def _shopify(instance: str):
    """A read-only client for `instance`, after checking the token belongs to its store."""
    from migtool.shopify.client import ShopifyClient

    inst = get_instance(instance, "shopify")
    shop = shopify_shop(inst)
    client = ShopifyClient(shop, credential(inst))
    try:
        info = client.shop_info()
        check_shop(inst, shop, info["domain"])
    except BaseException:
        client.close()
        raise
    return client, info


@shopify_app.command("whoami")
def shopify_whoami(instance: str = typer.Option(..., "--instance", help="Shopify instance to check.")) -> None:
    """Show the store a token belongs to and the scopes it was granted."""
    client, info = _shopify(instance)
    client.close()
    typer.echo(f"{instance}: {info['name']} ({info['domain']})")
    typer.echo(f"scopes: {', '.join(info['scopes']) or 'none'}")
    writes = [s for s in info["scopes"] if s.startswith("write_")]
    if writes:
        typer.echo(f"Note: the token also has write scopes ({', '.join(writes)}); this tool only reads, "
                   "except `shopify consent-sync` (email marketing consent).")


@shopify_app.command("customers-export")
def shopify_customers_export(
    instance: str = typer.Option(..., "--instance", help="Shopify instance to read."),
    fresh: bool = typer.Option(False, "--fresh", help="Start a new bulk export even if a recent one can be reused."),
) -> None:
    """Export every customer with their marketing consent, via a read-only bulk export.

    The export's Shopify ID is saved as soon as it starts, so an interrupted
    run continues the same export. A matching export from the last 24 hours
    (running or finished) is reused unless --fresh is given."""
    import json as _json
    from datetime import timedelta

    import httpx

    from migtool.shopify import customers as sc
    from migtool.shopify.client import _same_query

    store = StateStore()
    key = "shopify-customers-bulk"
    client, info = _shopify(instance)
    with client:
        op = None
        saved = store.load_checkpoint(instance, key)
        if saved and not fresh:
            op = client.bulk_operation(saved["id"])
            if op["status"] not in ("CREATED", "RUNNING", "COMPLETED"):
                op = None
            else:
                typer.echo(f"Continuing bulk export {op['id']} ({op['status'].lower()}).")
        if op is None and not fresh:
            cutoff = iso(utc_now() - timedelta(hours=24))
            recent = [o for o in client.recent_bulk_queries() if _same_query(o.get("query"), sc.BULK_QUERY)
                      and o["status"] in ("CREATED", "RUNNING", "COMPLETED") and (o.get("createdAt") or "") >= cutoff]
            if recent:
                op = recent[0]
                typer.echo(f"Reusing bulk export {op['id']} from {op['createdAt']} ({op['status'].lower()}); "
                           "--fresh starts a new one.")
        if op is None:
            op = {"id": client.start_bulk_export(sc.BULK_QUERY)}
            typer.echo(f"Started bulk export {op['id']}.")
        store.save_checkpoint(instance, key, {"id": op["id"], "store": info["domain"]})
        op = client.wait_bulk(op["id"], progress=typer.echo)
    run = new_run(instance, "customers-export")
    path = run.path(".csv")
    part = path.with_name(path.name + ".part")
    writer = CsvWriter(part, sc.EXPORT_COLUMNS)
    counts = {"customers": 0, "with_email": 0}
    try:
        if op.get("url"):
            with httpx.stream("GET", op["url"], timeout=300) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    row = sc.export_row(_json.loads(line))
                    writer.write(row)
                    counts["customers"] += 1
                    counts["with_email"] += bool(row["email"])
    finally:
        writer.close()
    if counts["customers"] != int(op.get("objectCount") or 0):
        raise ShopifyError(f"Downloaded {counts['customers']:,} customers but the export has "
                           f"{int(op.get('objectCount') or 0):,}; the partial file is {part}. Re-run to try again.")
    part.rename(path)
    store.clear_checkpoint(instance, key)
    write_manifest(run, files={path.name: writer.count}, counts=counts,
                   extra={"store": info["domain"], "bulk_operation": op["id"], "exported_at": op.get("createdAt")})
    typer.echo(f"Exported {counts['customers']:,} customers ({counts['with_email']:,} with an email) to {path}")


@shopify_app.command("customers")
def shopify_customers(
    instance: str = typer.Option(..., "--instance", help="Shopify instance to read."),
    email: list[str] = typer.Option([], "--email", help="An email to look up (repeatable)."),
    file: Path | None = typer.Option(None, "--file", exists=True, dir_okay=False, help="CSV with an email column."),
    compare: str | None = typer.Option(None, "--compare", help="Klaviyo instance to compare consent with."),
) -> None:
    """Look customers up by email: marketing consent, tags, orders, and optionally the Klaviyo profile."""
    from migtool.shopify import customers as sc

    emails = [e.strip().lower() for e in email if e.strip()]
    if file:
        _, rows = imports.read_rows(file)
        emails += [r["email"].strip().lower() for r in rows if (r.get("email") or "").strip()]
    emails = sorted(set(emails))
    if not emails:
        raise typer.BadParameter("give --email or --file.", param_hint="--email")
    client, info = _shopify(instance)
    with client:
        found = sc.customers_by_email(client, emails)
    profiles = {}
    if compare:
        with _client(compare) as kc:
            profiles = sc.klaviyo_profiles(kc, emails)
    run = new_run(instance, "customers")
    path = run.path(".csv")
    writer = CsvWriter(path, sc.COLUMNS)
    counts: dict[str, int] = {}
    for e in emails:
        r = sc.row(e, found.get(e), profiles.get(e) if compare else None)
        if not compare:
            r["match"] = "" if e in found else "no Shopify customer"
        counts[r["match"] or "found"] = counts.get(r["match"] or "found", 0) + 1
        writer.write(r)
    writer.close()
    write_manifest(run, files={path.name: writer.count}, counts=counts,
                   extra={"store": info["domain"], "compared_with": compare, "source_file": str(file) if file else None})
    typer.echo(f"{len(emails):,} emails, {len(found):,} Shopify customers found")
    for k, v in sorted(counts.items()):
        typer.echo(f"  {k}: {v:,}")
    typer.echo(f"Wrote {path}")

# --- Consent sync (docs/CONSENT_SYNC.md, BUILD_PLAN phase 6) ------------------------

CONSENT_BATCH = 50  # customers read (and written) per round
CONSENT_KEY = "shopify-consent-sync"
RESULT_COLUMNS = ["time", "shopify_customer_id", "email", "target_state", "target_date", "date_rule",
                  "state_before", "outcome", "detail"]
# A customer's latest outcome across a plan's runs decides what --resume does:
# done ones are skipped; unresolved ones are skipped and reported (and retried
# only with --retry-failed); `unknown` is always retried.
DONE_OUTCOMES = {"written", "skipped"}
UNRESOLVED_OUTCOMES = {"refused", "not found", "identity conflict", "ignored"}


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_rows(path: Path, columns: list[str], rows: list[dict]) -> int:
    w = CsvWriter(path, columns)
    for r in rows:
        w.write(r)
    w.close()
    return w.count


@shopify_app.command("consent-plan")
def shopify_consent_plan(
    instance: str = typer.Option("shopify_us", "--instance", help="Shopify instance the plan is for (output folder)."),
    klaviyo: Path = typer.Option(..., "--klaviyo", exists=True, dir_okay=False, help="Klaviyo `profiles export` CSV."),
    shopify: Path = typer.Option(..., "--shopify", exists=True, dir_okay=False, help="Shopify `customers-export` CSV."),
    emails: Path | None = typer.Option(None, "--emails", exists=True, dir_okay=False,
                                       help="CSV with an email column: plan only these (trials)."),
) -> None:
    """Plan the Klaviyo → Shopify email consent sync from two exports. Local and read-only.

    Writes <run>.plan.csv (the Shopify writes), <run>.klaviyo_d14.csv (D14:
    Klaviyo subscribes, for `klaviyo dedupe import --role consent`) and
    <run>.excluded.csv (D12 and unwritable customers). See docs/CONSENT_SYNC.md."""
    from migtool.shopify import consent

    get_instance(instance, "shopify")
    only = None
    if emails:
        try:
            _, rows = imports.read_rows(emails)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        only = {r["email"].strip().lower() for r in rows if r.get("email")}
    try:
        p = consent.plan(klaviyo, shopify, now=utc_now(), only=only)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    run = new_run(instance, "consent-plan")
    files = {}
    for suffix, columns, rows in ((".plan.csv", consent.PLAN_COLUMNS, p.writes),
                                  (".klaviyo_d14.csv", consent.D14_COLUMNS, p.d14),
                                  (".excluded.csv", consent.EXCLUDED_COLUMNS, p.excluded)):
        path = run.path(suffix)
        files[path.name] = _write_rows(path, columns, rows)
    by_target = {t: sum(1 for w in p.writes if w["target_state"] == t) for t in ("SUBSCRIBED", "UNSUBSCRIBED")}
    counts = {"klaviyo_profiles": p.profiles, "matched": p.matched, "shopify_writes": len(p.writes), **by_target,
              "dated_at_sync_time": p.sync_time,
              "klaviyo_d14": len(p.d14), "excluded": len(p.excluded), "shopify_newer": p.shopify_newer,
              "no_date": p.no_date, "future_dates_clamped": p.clamped,
              "by_transition": {" | ".join(k): v for k, v in sorted(p.counts.items())}}
    write_manifest(run, files=files, counts=counts,
                   extra={"klaviyo_export": str(klaviyo), "shopify_export": str(shopify),
                          "emails": str(emails) if emails else None})
    typer.echo(f"Klaviyo profiles {p.profiles:,}; matched to a Shopify customer by email {p.matched:,}")
    for (k, sh, act), n in sorted(p.counts.items()):
        typer.echo(f"  {k:12} | {sh:14} | {act:8} | {n:,}")
    typer.echo(f"Shopify writes: {len(p.writes):,} ({by_target['SUBSCRIBED']:,} SUBSCRIBED, "
               f"{by_target['UNSUBSCRIBED']:,} UNSUBSCRIBED); Shopify date newer than Klaviyo's: {p.shopify_newer:,}")
    typer.echo(f"Dated at the sync time (D16: Shopify's date is newer or equal, or there's no date): {p.sync_time:,}")
    typer.echo(f"Klaviyo writes (D14): {len(p.d14):,}; excluded: {len(p.excluded):,}")
    if p.no_date or p.clamped:
        typer.echo(f"Writes without a date: {p.no_date:,}; future dates set to now: {p.clamped:,}")
    for name in files:
        typer.echo(f"  {run.dir / name}")


@shopify_app.command("consent-sync")
def shopify_consent_sync(
    to: str = typer.Option(..., "--to", help="Shopify instance to write."),
    plan_file: Path = typer.Option(..., "--plan", exists=True, dir_okay=False, help="<run>.plan.csv from consent-plan."),
    target: str | None = typer.Option(None, "--target", help="Only rows with this target state (canary)."),
    limit: int | None = LIMIT,
    resume: bool = typer.Option(False, "--resume", help="Continue the saved run of this plan."),
    retry_failed: bool = typer.Option(False, "--retry-failed",
                                      help="With --resume, also retry customers refused, not found or in conflict."),
    yes: bool = YES,
) -> None:
    """Write Shopify email marketing consent from a consent plan. The only Shopify write.

    Each customer's current consent is read first; customers already in the
    target state are skipped. Only `customerEmailMarketingConsentUpdate` is
    sent. Per-customer results go to <run>.results.csv. A lost response isn't
    resent: the customer is read back instead."""
    from migtool.shopify import consent
    from migtool.shopify.client import CONSENT_STATES

    if target is not None and target not in CONSENT_STATES:
        raise typer.BadParameter(f"must be one of {', '.join(CONSENT_STATES)}", param_hint="--target")
    rows = list(consent.read_csv(plan_file))
    if rows and set(consent.PLAN_COLUMNS) - set(rows[0]):
        raise ConfigError(f"{plan_file} isn't a consent plan (missing {sorted(set(consent.PLAN_COLUMNS) - set(rows[0]))}).")
    bad = [r for r in rows if r["target_state"] not in CONSENT_STATES]
    if bad:
        raise ConfigError(f"{len(bad):,} plan rows have a target state Shopify can't take (e.g. {bad[0]['target_state']}).")
    if retry_failed and not resume:
        raise typer.BadParameter("needs --resume.", param_hint="--retry-failed")
    plan_key = str(plan_file.resolve())
    plan_hash = _file_sha256(plan_file)
    store = StateStore()
    saved = store.load_checkpoint(to, CONSENT_KEY)
    if saved and not resume:
        raise ConfigError(f"A consent sync of {saved['plan']} is saved. Use --resume to continue it, or delete "
                          f"state/{to}/{CONSENT_KEY}.checkpoint.json to start a new one.")
    if resume and not saved:
        raise ConfigError("There's no saved consent sync to resume.")
    if saved and (saved["plan"] != plan_key or saved.get("sha256") != plan_hash or saved.get("rows") != len(rows)):
        raise ConfigError(f"The saved consent sync is for {saved['plan']} as it was then ({saved.get('rows')} rows, "
                          f"sha256 {str(saved.get('sha256'))[:12]}); {plan_key} is different now. A changed plan "
                          "is a new run: review it and start it separately.")
    latest: dict[str, str] = {}
    for path in (saved or {}).get("results", []):
        for r in consent.read_csv(Path(path)):
            latest[r["shopify_customer_id"]] = r["outcome"]
    skip = {c for c, o in latest.items() if o in DONE_OUTCOMES or (o in UNRESOLVED_OUTCOMES and not retry_failed)}
    unresolved_before = {c for c, o in latest.items() if o in UNRESOLVED_OUTCOMES}
    todo = [r for r in rows if r["shopify_customer_id"] not in skip and (target is None or r["target_state"] == target)]
    if limit is not None:
        todo = todo[:limit]
    inst = get_instance(to, "shopify")
    client, info = _shopify(to)
    with client:
        if "write_customers" not in info["scopes"]:
            raise ConfigError(f"The {to} token doesn't have write_customers; nothing was written.")
        if saved and saved.get("store") != info["domain"]:
            raise ConfigError(f"The saved consent sync was against {saved.get('store')}, not {info['domain']}.")
        typer.echo(f"Plan:            {plan_file} ({len(rows):,} rows, "
                   f"{sum(o in DONE_OUTCOMES for o in latest.values()):,} already done)")
        if unresolved_before:
            typer.echo(f"Unresolved:      {len(unresolved_before):,} from earlier runs "
                       + ("(retried in this run)" if retry_failed else "(not retried; --retry-failed retries them)"))
        typer.echo(f"This run:        {len(todo):,} customers "
                   f"({sum(r['target_state'] == 'SUBSCRIBED' for r in todo):,} → SUBSCRIBED, "
                   f"{sum(r['target_state'] == 'UNSUBSCRIBED' for r in todo):,} → UNSUBSCRIBED)")
        typer.echo("Writes:          email marketing consent only (customerEmailMarketingConsentUpdate)")
        confirm_write(inst, account=f"{info['name']} ({info['domain']})", record_count=len(todo), yes=yes)
        run = new_run(to, "consent-sync")
        results = run.dir / f"{run.run_id}.results.csv"  # the run ID is unique even within one second
        if results.exists():
            raise ConfigError(f"{results} already exists; nothing was written.")
        writer = CsvWriter(results, RESULT_COLUMNS)
        store.save_checkpoint(to, CONSENT_KEY, {"plan": plan_key, "sha256": plan_hash, "rows": len(rows),
                                                "store": info["domain"],
                                                "results": [*(saved or {}).get("results", []), str(results)]})
        counts = {k: 0 for k in ("written", "skipped", "refused", "not found", "identity conflict", "ignored", "unknown")}
        started = utc_now()
        aborted = None

        def record(r: dict, before: str, outcome: str, detail: str = "", *, date: str = "", rule: str = "") -> None:
            counts[outcome] += 1
            writer.write({"time": iso(utc_now()), "shopify_customer_id": r["shopify_customer_id"], "email": r["email"],
                          "target_state": r["target_state"], "target_date": date, "date_rule": rule,
                          "state_before": before, "outcome": outcome, "detail": detail})

        def outcome_of(r: dict, stored: dict, sent: str) -> tuple[str, str]:
            """Written only if Shopify now holds the target state; Shopify ignores
            a change dated before its current consent date without an error."""
            state, at = stored.get("marketingState"), stored.get("consentUpdatedAt")
            if state != r["target_state"]:
                return "ignored", f"Shopify kept {state} {at}"
            if not consent.same_second(at, sent):
                return "written", f"stored {state} {at} (sent {sent})"
            return "written", f"stored {state} {at}"

        try:
            for i in range(0, len(todo), CONSENT_BATCH):
                batch = todo[i:i + CONSENT_BATCH]
                current = client.email_consents([r["shopify_customer_id"] for r in batch])
                for r in batch:
                    node = current.get(r["shopify_customer_id"])
                    if node is None:
                        record(r, "", "not found")
                        continue
                    live = node.get("emailMarketingConsent") or {}
                    before = live.get("marketingState") or ""
                    live_email = (node.get("email") or "").strip().lower()
                    if live_email != r["email"]:
                        # The plan's decision came from the Klaviyo profile for the planned email.
                        record(r, before, "identity conflict", f"Shopify email is now {node.get('email')!r}")
                        continue
                    if before == r["target_state"]:
                        record(r, before, "skipped", "already in the target state")
                        continue
                    # The original date, unless Shopify's live date is newer or equal (it
                    # would ignore the change) or there's none: then the sync time (D16).
                    now = utc_now().replace(microsecond=0)
                    date, rule = r["target_date"], consent.DATE_ORIGINAL
                    live_at = live.get("consentUpdatedAt")
                    if not date or parse_iso(date) > now or (live_at and parse_iso(live_at) >= parse_iso(date)):
                        date, rule = now.strftime("%Y-%m-%dT%H:%M:%SZ"), consent.DATE_SYNC_TIME
                    try:
                        result = client.update_email_consent(r["shopify_customer_id"], r["target_state"], date)
                    except ApiError as exc:
                        back = client.email_consents([r["shopify_customer_id"]]).get(r["shopify_customer_id"]) or {}
                        stored = back.get("emailMarketingConsent") or {}
                        if stored.get("marketingState") == r["target_state"]:
                            out, detail = outcome_of(r, stored, date)
                            record(r, before, out, f"response lost ({exc.status}); read back: {detail}", date=date, rule=rule)
                        else:
                            record(r, before, "unknown", f"response lost ({exc.status}); state now "
                                   f"{stored.get('marketingState')}", date=date, rule=rule)
                        continue
                    if result.get("userErrors"):
                        record(r, before, "refused", "; ".join(e.get("message", "") for e in result["userErrors"]),
                               date=date, rule=rule)
                    else:
                        out, detail = outcome_of(r, (result.get("customer") or {}).get("emailMarketingConsent") or {}, date)
                        record(r, before, out, detail, date=date, rule=rule)
                writer.flush()
                n = i + len(batch)
                if n % 1000 < CONSENT_BATCH or n == len(todo):
                    secs = max((utc_now() - started).total_seconds(), 1)
                    typer.echo(f"  {n:,}/{len(todo):,}: written {counts['written']:,}, skipped {counts['skipped']:,}, "
                               f"refused {counts['refused']:,} ({n / secs:.1f}/s)")
        except (Exception, KeyboardInterrupt) as exc:
            aborted = "interrupted" if isinstance(exc, KeyboardInterrupt) else f"{type(exc).__name__}: {exc}"
            typer.echo(f"Stopped: {aborted}. Re-run with --resume to continue.")
        finally:
            writer.close()
    secs = max((utc_now() - started).total_seconds(), 1)
    # Unresolved across the plan so far: this run's failures, plus earlier ones it didn't retry or resolve.
    retried = {r["shopify_customer_id"] for r in todo}
    unresolved = (unresolved_before - retried) | {
        r["shopify_customer_id"] for r in consent.read_csv(results)
        if r["outcome"] in UNRESOLVED_OUTCOMES or r["outcome"] == "unknown"}
    status = "aborted" if aborted else ("completed with errors" if unresolved else "complete")
    write_manifest(run, files={results.name: writer.count},
                   counts={**counts, "unresolved_in_plan": len(unresolved), "seconds": round(secs)}, status=status,
                   extra={"plan": plan_key, "plan_sha256": plan_hash, "store": info["domain"], "target": target,
                          "limit": limit, "retry_failed": retry_failed})
    typer.echo(f"written {counts['written']:,}, skipped {counts['skipped']:,}, refused {counts['refused']:,}, "
               f"not found {counts['not found']:,}, identity conflict {counts['identity conflict']:,}, "
               f"ignored {counts['ignored']:,}, unknown {counts['unknown']:,} in {round(secs):,}s")
    if unresolved:
        typer.echo(f"Unresolved in this plan so far: {len(unresolved):,} (see the results files)")
    typer.echo(f"Results: {results}")
    if aborted or unresolved:
        raise typer.Exit(1)


@shopify_app.command("consent-validate")
def shopify_consent_validate(
    instance: str = typer.Option("shopify_us", "--instance", help="Shopify instance (output folder)."),
    klaviyo: Path = typer.Option(..., "--klaviyo", exists=True, dir_okay=False, help="Klaviyo export after the run."),
    shopify: Path = typer.Option(..., "--shopify", exists=True, dir_okay=False, help="Shopify export after the run."),
    results: list[Path] = typer.Option([], "--results", exists=True, dir_okay=False,
                                       help="consent-sync results file (repeatable)."),
    d14: Path | None = typer.Option(None, "--d14", exists=True, dir_okay=False, help="<run>.klaviyo_d14.csv."),
    klaviyo_before: Path | None = typer.Option(None, "--klaviyo-before", exists=True, dir_okay=False,
                                               help="Klaviyo backup taken before the run."),
) -> None:
    """Check (local, read-only) that Shopify email consent mirrors Klaviyo after the sync.

    Exits 1 on any mismatch. Klaviyo consent changes since the backup are
    listed for review (expected for D14 customers and normal activity)."""
    from migtool.shopify import consent

    get_instance(instance, "shopify")
    d14_emails = {r["email"] for r in consent.read_csv(d14)} if d14 else set()
    v = consent.validate(klaviyo, shopify, written=consent.written_targets(results), d14_emails=d14_emails,
                         klaviyo_before=klaviyo_before)
    run = new_run(instance, "consent-validate")
    files = {}
    if v.mismatches:
        path = run.path(".mismatches.csv")
        files[path.name] = _write_rows(path, consent.MISMATCH_COLUMNS, v.mismatches)
    if v.klaviyo_changes:
        path = run.path(".klaviyo_changes.csv")
        files[path.name] = _write_rows(path, consent.KLAVIYO_CHANGE_COLUMNS, v.klaviyo_changes)
    write_manifest(run, files=files, counts=dict(v.counts),
                   extra={"klaviyo": str(klaviyo), "shopify": str(shopify), "results": [str(r) for r in results],
                          "d14": str(d14) if d14 else None, "klaviyo_before": str(klaviyo_before) if klaviyo_before else None})
    c = v.counts
    typer.echo(f"checked {c['checked']:,}: ok {c['ok']:,}, mismatched {c['mismatched']:,} "
               f"(D12 {c['d12']:,}, excluded {c['excluded']:,})")
    if results:
        typer.echo(f"written by the run: {c['written_checked']:,} checked, {c['written_mismatched']:,} mismatched")
    if klaviyo_before:
        typer.echo(f"Klaviyo consent changes since the backup: {c['klaviyo_changed']:,} (D14: {c['klaviyo_changed_d14']:,}); "
                   f"profiles gone (deleted or merged): {c['klaviyo_missing']:,}")
    for name in files:
        typer.echo(f"  {run.dir / name}")
    if v.mismatches:
        raise typer.Exit(1)


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
    except ShopifyError as exc:
        typer.echo(f"Shopify error: {exc}", err=True)
        sys.exit(1)
