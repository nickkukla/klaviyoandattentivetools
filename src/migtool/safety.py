"""Write confirmation: every write names its target and is confirmed first."""

from __future__ import annotations

from collections.abc import Callable

import typer

from migtool.config import Instance


class WriteRefused(Exception):
    """The write was not confirmed, or is not allowed with the given flags."""


def confirm_write(
    target: Instance,
    *,
    account: str,
    record_count: int,
    yes: bool = False,
    allow_write_to_source: bool = False,
    echo: Callable[[str], None] = typer.echo,
    prompt: Callable[[str], str] = typer.prompt,
) -> None:
    """Show what is about to be written and where, then require a typed confirmation.

    Writes to a `_ca` (source) instance need `allow_write_to_source`, even with
    `yes`. `yes` skips only the typed confirmation.
    """
    if target.is_source and not allow_write_to_source:
        raise WriteRefused(
            f"'{target.name}' is a source instance. Writing to it needs --allow-write-to-source."
        )
    echo(f"Target instance: {target.name}")
    echo(f"Account:         {account}")
    echo(f"Records:         {record_count:,}")
    if yes:
        return
    typed = prompt(f"Type the instance name ({target.name}) to confirm")
    if typed.strip() != target.name:
        raise WriteRefused("Confirmation did not match the instance name. Nothing was written.")
