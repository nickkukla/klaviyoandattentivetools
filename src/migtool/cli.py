"""`migtool` command line. Service commands are added phase by phase."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import typer

from migtool.config import INSTANCES, ConfigError, check_account, credential, get_instance, load_env
from migtool.http import ApiError
from migtool.klaviyo import bis, dedupe, groups, imports, profiles, segments, suppressions
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
dedupe_app = typer.Typer(no_args_is_help=True, help="Import the dedupe files (Klaviyo UI-import layout).")
klaviyo_app.add_typer(profiles_app, name="profiles")
klaviyo_app.add_typer(lists_app, name="lists")
klaviyo_app.add_typer(segments_app, name="segments")
klaviyo_app.add_typer(suppressions_app, name="suppressions")
klaviyo_app.add_typer(bis_app, name="bis")
klaviyo_app.add_typer(dedupe_app, name="dedupe")

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


def _gate_pending_jobs(client: KlaviyoClient, inst) -> None:
    """Stop a write while a profile import job an earlier run submitted is
    still processing (after Ctrl-C, a polling failure or a timeout): Klaviyo
    doesn't guarantee order, so it could land after this write and undo it.
    Saved jobs are looked up once and their final status recorded."""
    store = StateStore()
    open_jobs = [j for j in store.jobs(inst.name)
                 if j.get("kind") == "profile-bulk-import-jobs" and j.get("status") not in JOB_DONE]
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
    phones: bool = False,
) -> None:
    """Shared frame for the write commands: read and check the file, confirm the
    target, run `body(importer, rows, columns, run)`, then write the skipped
    file, manifest and summary. Exits non-zero if anything failed."""
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
        typer.echo(f"File:            {file} ({len(raw):,} rows, {len(batch.skipped):,} skipped)")
        confirm_write(
            inst, account=f"{account['name']} ({account['id']})", record_count=len(batch.rows),
            yes=yes, allow_write_to_source=allow_write_to_source,
        )
        log = RunLog(run, written_label="submitted" if main_step == "suppress" else "written")
        log.read(len(raw))
        log.skipped(len(batch.skipped))

        def save_job(job: Job) -> None:
            store.add_job(inst.name, {"id": job.id, "kind": job.kind, "run_id": run.run_id, "size": job.size})

        imp = imports.Importer(Writer(client), log, echo=typer.echo, save_job=save_job, main_step=main_step)
        counts: dict = {}
        aborted: str | None = None
        try:
            counts = body(imp, batch.rows, columns, run) or {}
        except (Exception, KeyboardInterrupt) as exc:
            # Stop, but still record what was sent: earlier jobs may be running.
            aborted = "interrupted" if isinstance(exc, KeyboardInterrupt) else f"{type(exc).__name__}: {exc}"
            log.error("run", f"stopped: {aborted}. Jobs already submitted are in state/ and may still "
                      "be applied; re-running the file is safe.", stage="aborted")
        for job in imp.unfinished:
            typer.echo(f"Job {job.id} ({job.kind}) was still {job.status or 'processing'} when the run ended.")
    skipped_path = imports.write_skipped(run, batch.skipped)
    files = {skipped_path.name: len(batch.skipped)} if skipped_path else {}
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
) -> None:
    """Add every profile in the file to one list, by email or (phone-only rows) phone number; writes
    consent only if the file has consent columns."""

    def body(imp, rows, columns, run):
        return {"created": imports.lists_add(imp, rows, columns, list_id=list_id, run_id=run.run_id)}

    _write_run(to, "lists-add", "add", file, limit, yes, allow_write_to_source, body, {"list_id": list_id},
               retries_settled=retries_settled, phones=True)



@lists_app.command("copy")
def lists_copy(
    from_: str = typer.Option(..., "--from", help="Klaviyo instance to read the list from (read-only)."),
    source_list: str = typer.Option(..., "--list", help="ID of the list to copy."),
    to: str = TO,
    to_list: str | None = typer.Option(None, "--to-list", help="ID of an existing list to add the members to."),
    create: bool = typer.Option(False, "--create", help="Create the destination list instead."),
    name: str | None = typer.Option(
        None, "--name", help="Name of the list --create makes (default: the source list's name plus --suffix)."
    ),
    suffix: str = typer.Option(" (CA)", "--suffix", help="Added to the source list's name when --name isn't given."),
    limit: int | None = LIMIT,
    yes: bool = YES,
    allow_write_to_source: bool = ALLOW_SOURCE,
    retries_settled: bool = RETRIES_SETTLED,
) -> None:
    """Copy one list's members into a list on another instance, creating it if asked.

    Members are matched by email, or by phone number when they have no email,
    and only added to the list: no field, property or consent changes. A member
    with no profile at the destination is created, with the migration tags."""
    if (to_list is None) != create:
        raise typer.BadParameter("give exactly one of --to-list or --create.", param_hint="--to-list")
    if name and not create:
        raise typer.BadParameter("--name only applies with --create.", param_hint="--name")
    # Read the source list's members (read-only) into an email-only file.
    source = new_run(from_, "lists-copy")
    members_path = source.path(".members.csv")
    with _client(from_) as client:
        try:
            group = client.get(f"/lists/{source_list}/", tier="S", params={"fields[list]": "name"})["data"]
        except ApiError as exc:
            raise ConfigError(f"List {source_list} wasn't found on {from_} ({exc.status}).") from exc
        source_name = group["attributes"]["name"]
        # The phone only for phone-only members: it's their identifier. For the
        # others, sending it could change the destination profile's number.
        writer = CsvWriter(members_path, ["email", "phone_number"])
        phone_only = neither = 0
        params = {"fields[profile]": "email,phone_number", "page[size]": "100"}
        for page in client.paginate(f"/lists/{source_list}/profiles/", tier="L", params=params):
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
                   extra={"list_id": source_list, "list_name": source_name})
    typer.echo(f"Source list:     {source_name} ({source_list}) on {from_}: {writer.count + neither:,} members "
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
    extra = {"source_instance": from_, "source_list": source_list, "source_members_file": str(members_path),
             "list_id": to_list, "list_name": dest_name}

    def body(imp, rows, columns, run):
        list_id = to_list
        if not list_id:
            list_id = imp.w.create_list(dest_name)
            extra["list_id"] = list_id
            typer.echo(f"Created list {dest_name} ({list_id})")
        return {"created": imports.lists_add(imp, rows, columns, list_id=list_id, run_id=run.run_id)}

    _write_run(to, "lists-copy", "add", members_path, limit, yes, allow_write_to_source, body, extra,
               retries_settled=retries_settled, phones=True)

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
        main_step = {"hold": "update", "kept": "update", "suppress": "suppress"}.get(role, "import")
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
