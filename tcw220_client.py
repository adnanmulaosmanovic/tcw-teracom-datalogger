"""Komunikacija s Teracom TCW220 dataloggerom.

Koristi dva HTTP izvora koje uređaj sam nudi:
  * status.json                    - trenutne vrijednosti svih ulaza
  * logger/log.json?lstart&lend&llist - snimljeni (arhivirani) zapisi

Ovaj modul ne zna ništa o grafičkom sučelju.
"""
from __future__ import annotations

import calendar
import csv
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable, Optional

import requests
from requests.auth import HTTPBasicAuth

# Grupe mjerenja
GROUP_SENSORS = "sensors"   # 1-Wire senzori (temperatura, vlaga, ...)
GROUP_ANALOG = "analog"     # analogni ulazi 0-10 V
GROUP_DIGITAL = "digital"   # digitalni ulazi
GROUP_RELAYS = "relays"     # releji

# Uređaj dopušta najviše 5 signala po upitu loga (toliko ih nudi i njegova web stranica)
MAX_SIGNALS_PER_REQUEST = 5
# Veliki periodi se preuzimaju u manjim komadima da se uređaj ne optereti
LOG_CHUNK_DAYS = 3

_UNIT_LABELS = {
    "°C": "Temperatura",
    "%RH": "Vlaga",
    "hPa": "Pritisak",
    "mbar": "Pritisak",
    "kPa": "Pritisak",
    "bar": "Pritisak",
}


class DeviceError(Exception):
    """Greška u komunikaciji s uređajem. Poruka je razumljiva korisniku."""


@dataclass
class Reading:
    key: str                      # npr. "S1.1", "AI1", "DI2", "R1"
    group: str
    name: str
    value_text: str               # vrijednost kako je uređaj šalje ("27.1", "OPEN", "ON")
    unit: str
    value: Optional[float]
    alarm: bool
    log_index: Optional[int]      # broj signala za upit loga (llist), ako postoji


@dataclass
class Snapshot:
    device_name: str = ""
    sys_name: str = ""
    sys_location: str = ""
    firmware: str = ""
    device_time: str = ""
    hw_error: str = ""
    fetched_at: datetime = field(default_factory=datetime.now)
    readings: list = field(default_factory=list)


@dataclass
class LogColumn:
    index: int
    title: str
    unit: str
    digits: int


@dataclass
class LogData:
    columns: list                 # list[LogColumn]
    rows: list                    # list[(datetime, list[Optional[float]], list[bool])]


# --------------------------------------------------------------------------- #
# Pomoćne funkcije
# --------------------------------------------------------------------------- #
def _num_suffix(key: str) -> int:
    m = re.search(r"(\d+)$", key)
    return int(m.group(1)) if m else 0


def _to_float(text) -> Optional[float]:
    try:
        return float(str(text).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None


def _is_empty(text: str) -> bool:
    return text.strip() in ("", "---")


def _to_epoch(dt: datetime) -> int:
    """Uređaj bilježi vrijeme kao 'goli' unix broj lokalnog sata uređaja (bez vremenske zone)."""
    return calendar.timegm(dt.timetuple())


def _from_epoch(ts: int) -> datetime:
    return datetime(1970, 1, 1) + timedelta(seconds=int(ts))


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:  # neki firmveri šalju zarez ispred zatvorene zagrade
        return json.loads(re.sub(r",\s*([}\]])", r"\1", text.strip()))
    except ValueError as exc:
        raise DeviceError("Uređaj je vratio neispravan odgovor.") from exc


# --------------------------------------------------------------------------- #
# Parsiranje statusa
# --------------------------------------------------------------------------- #
def parse_status(data: dict) -> Snapshot:
    mon = data.get("Monitor")
    if not isinstance(mon, dict):
        raise DeviceError("Neočekivan odgovor uređaja (nema podatka 'Monitor').")

    info = mon.get("DeviceInfo") or {}
    snap = Snapshot(
        device_name=str(info.get("DeviceName", "")),
        sys_name=str(info.get("SysName", "")),
        sys_location=str(info.get("SysLocation", "")),
        firmware=str(info.get("FwVer", "")),
        hw_error=str(mon.get("hwerr") or ""),
    )
    t = mon.get("Time") or {}
    snap.device_time = f"{t.get('Date', '')} {t.get('Time', '')}".strip()

    readings: list[Reading] = []

    # 1-Wire senzori S1..S8, svaki do dvije veličine (item1, item2)
    sensors = mon.get("S") or {}
    for skey in sorted(sensors, key=_num_suffix):
        if not re.fullmatch(r"S\d+", skey):
            continue
        n = _num_suffix(skey)
        sdata = sensors[skey]
        if not isinstance(sdata, dict):
            continue
        desc = str(sdata.get("description") or skey)
        for ikey in sorted((k for k in sdata if re.fullmatch(r"item\d+", k)), key=_num_suffix):
            item = sdata[ikey]
            if not isinstance(item, dict):
                continue
            k = _num_suffix(ikey)
            vtext = str(item.get("value", "---"))
            if _is_empty(vtext):
                continue                      # nepovezan senzor
            unit = str(item.get("unit", "")).strip()
            unit = "" if unit == "---" else unit
            label = _UNIT_LABELS.get(unit, unit or f"Mjerenje {k}")
            readings.append(Reading(
                key=f"S{n}.{k}", group=GROUP_SENSORS, name=f"{desc} – {label}",
                value_text=vtext.strip(), unit=unit, value=_to_float(vtext),
                alarm=str(item.get("alarm", "0")) == "1",
                log_index=(n - 1) * 2 + k if k in (1, 2) and n <= 8 else None))

    # Analogni ulazi (log signali 17, 18)
    for key, a in sorted((mon.get("AI") or {}).items(), key=lambda kv: _num_suffix(kv[0])):
        if not isinstance(a, dict) or _is_empty(str(a.get("value", "---"))):
            continue
        n = _num_suffix(key)
        unit = str(a.get("unit", "")).strip()
        readings.append(Reading(
            key=f"AI{n}", group=GROUP_ANALOG, name=str(a.get("description") or key),
            value_text=str(a["value"]).strip(), unit="" if unit == "---" else unit,
            value=_to_float(a["value"]), alarm=str(a.get("alarm", "0")) == "1",
            log_index=16 + n))

    # Digitalni ulazi (log signali 19, 20)
    for key, d in sorted((mon.get("DI") or {}).items(), key=lambda kv: _num_suffix(kv[0])):
        if not isinstance(d, dict):
            continue
        n = _num_suffix(key)
        readings.append(Reading(
            key=f"DI{n}", group=GROUP_DIGITAL, name=str(d.get("description") or key),
            value_text=str(d.get("value", "")).strip(), unit="", value=_to_float(d.get("valuebin")),
            alarm=str(d.get("alarm", "0")) == "1", log_index=18 + n))

    # Releji (log signali 21, 22)
    for key, r in sorted((mon.get("R") or {}).items(), key=lambda kv: _num_suffix(kv[0])):
        if not isinstance(r, dict):
            continue
        n = _num_suffix(key)
        readings.append(Reading(
            key=f"R{n}", group=GROUP_RELAYS, name=str(r.get("description") or key),
            value_text=str(r.get("value", "")).strip(), unit="", value=_to_float(r.get("valuebin")),
            alarm=False, log_index=20 + n))

    snap.readings = readings
    return snap


def prepare_readings(readings: list, groups, hide, names: dict) -> list:
    """Primijeni postavke iz config.json: filtriranje grupa, skrivanje i vlastita imena."""
    hide = {h.upper() for h in hide}
    out = []
    for r in readings:
        if r.group not in groups or r.key.upper() in hide:
            continue
        custom = names.get(r.key.upper())
        out.append(replace(r, name=custom) if custom else r)
    return out


# --------------------------------------------------------------------------- #
# Klijent
# --------------------------------------------------------------------------- #
class TCW220Client:
    def __init__(self, host: str, port: int = 80, username: str = "admin",
                 password: str = "", timeout: float = 8.0, https: bool = False):
        scheme = "https" if https else "http"
        self.host = host
        self.port = port
        self.base_url = f"{scheme}://{host}:{port}"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.headers["Connection"] = "close"   # embedded uređaj: bez dugih veza
        if https:
            self.session.verify = False                # uređaji koriste samopotpisane certifikate

    def _request(self, path: str, timeout: Optional[float] = None) -> str:
        url = f"{self.base_url}/{path}"
        try:
            r = self.session.get(url, timeout=timeout or self.timeout)
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout):
            raise DeviceError("Uređaj ne odgovara (isteklo vrijeme čekanja).")
        except requests.exceptions.ConnectionError:
            raise DeviceError(f"Nije moguće spojiti se na {self.host}:{self.port}. "
                              "Provjerite adresu i mrežu.")
        except requests.exceptions.RequestException as exc:
            raise DeviceError(f"Greška u komunikaciji: {exc}")
        if r.status_code == 401:
            raise DeviceError("Pogrešno korisničko ime ili lozinka.")
        if r.status_code != 200:
            raise DeviceError(f"Uređaj je vratio HTTP {r.status_code}.")
        # Uređaj šalje UTF-8 s BOM oznakom
        return r.content.decode("utf-8-sig", errors="replace")

    def get_status(self) -> Snapshot:
        snap = parse_status(_parse_json(self._request("status.json")))
        snap.fetched_at = datetime.now()
        return snap

    def get_log(self, start: datetime, end: datetime, indices: list,
                titles: Optional[dict] = None,
                progress: Optional[Callable[[int, int], None]] = None,
                cancel=None) -> LogData:
        """Preuzmi snimljene zapise u periodu [start, end] za zadane signale."""
        if not indices:
            raise DeviceError("Nije odabrano nijedno mjerenje.")
        if start > end:
            raise DeviceError("Početni datum je poslije završnog.")
        titles = titles or {}

        groups = [indices[i:i + MAX_SIGNALS_PER_REQUEST]
                  for i in range(0, len(indices), MAX_SIGNALS_PER_REQUEST)]
        # Periodi se lagano preklapaju (1 s) pa se ne gubi zapis na granici bez obzira
        # tretira li uređaj početak kao uključen ili isključen; duplikati se uklanjaju.
        windows = []
        cur = start - timedelta(seconds=1)
        while cur < end:
            nxt = min(cur + timedelta(days=LOG_CHUNK_DAYS), end)
            windows.append((cur, nxt))
            cur = nxt

        total = len(windows) * len(groups)
        done = 0
        data: dict = {}            # datetime -> {indeks: (vrijednost, alarm)}
        meta: dict = {}            # indeks -> LogColumn
        for w0, w1 in windows:
            for grp in groups:
                if cancel is not None and cancel.is_set():
                    raise DeviceError("Preuzimanje je prekinuto.")
                self._fetch_log_chunk(w0, w1, grp, data, meta, titles)
                done += 1
                if progress:
                    progress(done, total)

        columns = [meta[i] for i in indices if i in meta]
        rows = []
        for ts in sorted(data):
            if ts < start or ts > end:
                continue
            cell = data[ts]
            rows.append((ts,
                         [cell.get(c.index, (None, False))[0] for c in columns],
                         [cell.get(c.index, (None, False))[1] for c in columns]))
        return LogData(columns=columns, rows=rows)

    def _fetch_log_chunk(self, w0, w1, grp, data, meta, titles):
        path = (f"logger/log.json?lstart={_to_epoch(w0)}&lend={_to_epoch(w1)}"
                f"&llist={','.join(str(i) for i in grp)}")
        j = _parse_json(self._request(path, timeout=max(self.timeout, 60)))
        log = j.get("Log")
        if not isinstance(log, dict):
            raise DeviceError("Neočekivan odgovor uređaja pri čitanju loga.")
        header = log.get("Header") or []
        rows = log.get("data") or []

        idxs = []
        for pos, h in enumerate(header):
            try:
                idx = int(h.get("Idx"))
            except (TypeError, ValueError):
                idx = grp[pos] if pos < len(grp) else pos + 1
            idxs.append(idx)
            if idx not in meta:
                unit = str(h.get("Unit", "")).strip()
                digits = int(_to_float(h.get("Digits", 1)) or 0)
                title = titles.get(idx)
                if not title:
                    title = f"{h.get('Description', idx)}" + (f" [{unit}]" if unit else "")
                meta[idx] = LogColumn(idx, title, unit, digits)

        w = len(header)
        for row in rows:
            if not row or len(row) < 1 + w:
                continue
            ts = _from_epoch(row[0])
            alarms = row[1 + w:1 + 2 * w]
            cell = data.setdefault(ts, {})
            for pos, idx in enumerate(idxs):
                raw = row[1 + pos]
                value = None if raw == "E" else _to_float(raw)
                alarm = pos < len(alarms) and str(alarms[pos]) == "1"
                cell[idx] = (value, alarm)


# --------------------------------------------------------------------------- #
# Izvoz u CSV (otvara se direktno u Excelu)
# --------------------------------------------------------------------------- #
def format_number(value: float, digits: int, decimal: str = ",") -> str:
    return f"{value:.{max(digits, 0)}f}".replace(".", decimal)


def export_csv(path: str, log: LogData, decimal: str = ",") -> int:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Vrijeme"] + [c.title for c in log.columns] + ["Alarm"])
        for ts, values, alarms in log.rows:
            out = [ts.strftime("%d.%m.%Y %H:%M:%S")]
            for c, v in zip(log.columns, values):
                out.append("" if v is None else format_number(v, c.digits, decimal))
            out.append("; ".join(c.title for c, a in zip(log.columns, alarms) if a))
            w.writerow(out)
    return len(log.rows)
