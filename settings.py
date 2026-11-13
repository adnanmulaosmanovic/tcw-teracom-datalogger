"""Učitavanje i provjera konfiguracijskog fajla (config.json)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

CONFIG_FILE_NAME = "config.json"
VALID_GROUPS = ("sensors", "analog", "digital", "relays")
MIN_REFRESH_SECONDS = 5

SAMPLE_CONFIG = """{
  "refresh_seconds": 30,
  "timeout_seconds": 8,
  "archive_folder": "arhiva",
  "decimal_separator": ",",
  "record_live": false,

  "dataloggers": [
    {
      "name": "Blok 6",
      "host": "192.168.1.2",
      "port": 80,
      "username": "admin",
      "password": "admin",
      "enabled": true,
      "groups": ["sensors"],
      "hide_channels": [],
      "channel_names": {
        "S1.1": "Temperatura",
        "S1.2": "Vlaga"
      }
    },
    {
      "name": "Primjer drugog dataloggera",
      "host": "datalogger2.firma.local",
      "port": 80,
      "username": "admin",
      "password": "admin",
      "enabled": false
    }
  ]
}
"""


class ConfigError(Exception):
    """Greška u konfiguraciji (poruka je razumljiva korisniku)."""


def app_dir() -> str:
    """Folder u kojem je exe (ili skripta). Tu se traži config.json i tu se piše log."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


@dataclass
class DataloggerConfig:
    name: str
    host: str
    port: int = 80
    username: str = "admin"
    password: str = ""
    https: bool = False
    enabled: bool = True
    groups: list = field(default_factory=lambda: ["sensors"])
    hide_channels: list = field(default_factory=list)
    channel_names: dict = field(default_factory=dict)


@dataclass
class AppConfig:
    refresh_seconds: int = 30
    timeout_seconds: int = 8
    archive_folder: str = ""
    decimal_separator: str = ","
    record_live: bool = False
    dataloggers: list = field(default_factory=list)
    path: str = ""


def default_config_path() -> str:
    return os.path.join(app_dir(), CONFIG_FILE_NAME)


def write_sample_config(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(SAMPLE_CONFIG)


def _as(value, typ, label, default):
    if value is None:
        return default
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"'{label}' mora biti broj.")
        return int(value)
    if typ is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"'{label}' mora biti true ili false.")
        return value
    if typ is str:
        if not isinstance(value, str):
            raise ConfigError(f"'{label}' mora biti tekst u navodnicima.")
        return value
    return value


def load_config(path: str | None = None) -> AppConfig:
    path = path or default_config_path()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:   # utf-8-sig: Notepad dodaje BOM
            raw = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Konfiguracijski fajl nije pronađen:\n{path}")
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Greška u fajlu config.json (red {exc.lineno}, kolona {exc.colno}):\n{exc.msg}\n\n"
            "Najčešći uzroci: nedostaje zarez između dataloggera, višak zareza na kraju "
            "liste ili nedostaju navodnici.")
    if not isinstance(raw, dict):
        raise ConfigError("config.json mora počinjati sa { i završavati sa }.")

    cfg = AppConfig(path=path)
    cfg.refresh_seconds = _as(raw.get("refresh_seconds"), int, "refresh_seconds", 30)
    if cfg.refresh_seconds < MIN_REFRESH_SECONDS:
        raise ConfigError(f"'refresh_seconds' ne smije biti manji od {MIN_REFRESH_SECONDS} sekundi.")
    cfg.timeout_seconds = max(2, _as(raw.get("timeout_seconds"), int, "timeout_seconds", 8))
    cfg.decimal_separator = _as(raw.get("decimal_separator"), str, "decimal_separator", ",")
    if cfg.decimal_separator not in (",", "."):
        raise ConfigError("'decimal_separator' mora biti \",\" ili \".\".")
    cfg.record_live = _as(raw.get("record_live"), bool, "record_live", False)

    folder = _as(raw.get("archive_folder"), str, "archive_folder", "arhiva") or "arhiva"
    cfg.archive_folder = folder if os.path.isabs(folder) else os.path.join(app_dir(), folder)

    items = raw.get("dataloggers")
    if not isinstance(items, list) or not items:
        raise ConfigError("U config.json nema nijednog dataloggera ('dataloggers').")

    seen = set()
    for pos, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"Datalogger broj {pos} nije ispravno zapisan.")
        name = _as(item.get("name"), str, f"dataloggers[{pos}].name", "").strip()
        host = _as(item.get("host"), str, f"dataloggers[{pos}].host", "").strip()
        for prefix in ("http://", "https://"):
            if host.lower().startswith(prefix):
                host = host[len(prefix):]
        host = host.rstrip("/")
        if not name or not host:
            raise ConfigError(f"Datalogger broj {pos}: obavezna polja su 'name' i 'host'.")
        if name.lower() in seen:
            raise ConfigError(f"Naziv dataloggera '{name}' se ponavlja. Svaki naziv mora biti jedinstven.")
        seen.add(name.lower())

        groups = item.get("groups", ["sensors"])
        if not isinstance(groups, list) or any(g not in VALID_GROUPS for g in groups):
            raise ConfigError(f"Datalogger '{name}': 'groups' može sadržavati samo: "
                              + ", ".join(VALID_GROUPS) + ".")
        names = item.get("channel_names", {})
        if not isinstance(names, dict):
            raise ConfigError(f"Datalogger '{name}': 'channel_names' mora biti oblika {{\"S1.1\": \"Naziv\"}}.")
        hide = item.get("hide_channels", [])
        if not isinstance(hide, list):
            raise ConfigError(f"Datalogger '{name}': 'hide_channels' mora biti lista.")

        cfg.dataloggers.append(DataloggerConfig(
            name=name, host=host,
            port=_as(item.get("port"), int, f"{name}.port", 80),
            username=_as(item.get("username"), str, f"{name}.username", "admin"),
            password=_as(item.get("password"), str, f"{name}.password", ""),
            https=_as(item.get("https"), bool, f"{name}.https", False),
            enabled=_as(item.get("enabled"), bool, f"{name}.enabled", True),
            groups=groups, hide_channels=[str(h) for h in hide],
            channel_names={str(k).upper(): str(v) for k, v in names.items()}))

    if not any(d.enabled for d in cfg.dataloggers):
        raise ConfigError("Svi dataloggeri su isključeni ('enabled': false).")
    return cfg
