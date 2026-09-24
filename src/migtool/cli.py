"""`migtool` command line. Service commands are added phase by phase."""

from __future__ import annotations

import os
import sys

import typer

from migtool.config import INSTANCES, ConfigError, credential, get_instance, load_env
from migtool.klaviyo.client import KlaviyoClient
from migtool.http import ApiError
from migtool.output import ResumeError
from migtool.safety import WriteRefused

# Locals are hidden in tracebacks so a crash can't print a key.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
klaviyo_app = typer.Typer(no_args_is_help=True, help="Klaviyo exports and imports.")
app.add_typer(klaviyo_app, name="klaviyo")


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
