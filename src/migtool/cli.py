"""`migtool` command line. Service commands are added phase by phase."""

from __future__ import annotations

import os
import sys
from datetime import datetime

import typer

from migtool.config import INSTANCES, ConfigError, credential, get_instance, load_env
from migtool.http import ApiError
from migtool.klaviyo import groups, profiles, segments, suppressions
from migtool.klaviyo.client import KlaviyoClient
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
from migtool.safety import WriteRefused

# Locals are hidden in tracebacks so a crash can't print a key.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
klaviyo_app = typer.Typer(no_args_is_help=True, help="Klaviyo exports and imports.")
app.add_typer(klaviyo_app, name="klaviyo")
profiles_app = typer.Typer(no_args_is_help=True, help="Profiles.")
lists_app = typer.Typer(no_args_is_help=True, help="Lists and their members.")
segments_app = typer.Typer(no_args_is_help=True, help="Segments and their members.")
suppressions_app = typer.Typer(no_args_is_help=True, help="Email suppressions.")
klaviyo_app.add_typer(profiles_app, name="profiles")
klaviyo_app.add_typer(lists_app, name="lists")
klaviyo_app.add_typer(segments_app, name="segments")
klaviyo_app.add_typer(suppressions_app, name="suppressions")

INSTANCE = typer.Option(..., "--instance", help="Klaviyo instance to export from.")
SINCE = typer.Option(
    None, "--since", help="Only records changed after this UTC ISO 8601 time, e.g. 2026-09-24T15:30:00Z."
)
RESUME = typer.Option(False, "--resume", help="Continue the unfinished export with the same options.")


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
def lists_export(instance: str = INSTANCE, since: str | None = SINCE) -> None:
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
