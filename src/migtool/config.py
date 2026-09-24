"""Instance registry and .env loading.

Every command targets a named instance. Each instance maps to one `.env`
variable holding its API key (Klaviyo, Attentive) or shop domain (STOQ).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """A configuration problem the user must fix, such as a missing variable."""


class Secret:
    """A credential that never shows its value in logs, errors or reprs."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('****')"

    __str__ = __repr__


@dataclass(frozen=True)
class Instance:
    name: str
    service: str  # "klaviyo" | "attentive" | "stoq"
    env_var: str

    @property
    def is_source(self) -> bool:
        """Canada instances are migration sources; writes to them need an extra flag."""
        return self.name.endswith("_ca")


INSTANCES: dict[str, Instance] = {
    i.name: i
    for i in (
        Instance("klaviyo_ca", "klaviyo", "KLAVIYO_CA_API_KEY"),
        Instance("klaviyo_us", "klaviyo", "KLAVIYO_US_API_KEY"),
        Instance("klaviyo_sandbox", "klaviyo", "KLAVIYO_SANDBOX_API_KEY"),
        Instance("attentive_ca", "attentive", "ATTENTIVE_CA_API_KEY"),
        Instance("attentive_us", "attentive", "ATTENTIVE_US_API_KEY"),
        Instance("stoq_dev", "stoq", "STOQ_DEV_SHOP_DOMAIN"),
        Instance("stoq_us", "stoq", "STOQ_US_SHOP_DOMAIN"),
    )
}


def load_env(path: Path = Path(".env")) -> None:
    """Load `.env` into the process environment. Real environment variables win."""
    load_dotenv(path, override=False)


def get_instance(name: str, service: str | None = None) -> Instance:
    """Look up an instance by name, optionally requiring it to belong to `service`."""
    inst = INSTANCES.get(name)
    if inst is None:
        valid = ", ".join(sorted(INSTANCES))
        raise ConfigError(f"Unknown instance '{name}'. Valid instances: {valid}")
    if service is not None and inst.service != service:
        valid = ", ".join(sorted(n for n, i in INSTANCES.items() if i.service == service))
        raise ConfigError(f"'{name}' is not a {service} instance. Valid instances: {valid}")
    return inst


def credential(inst: Instance, environ: Mapping[str, str] | None = None) -> Secret:
    """The instance's API key or shop domain, or a ConfigError naming the missing variable."""
    env = os.environ if environ is None else environ
    value = env.get(inst.env_var, "").strip()
    if not value:
        raise ConfigError(
            f"{inst.env_var} is not set (needed for instance '{inst.name}'). Add it to .env."
        )
    return Secret(value)
