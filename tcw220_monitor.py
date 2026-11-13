"""TCW220 Monitor - pregled i arhiva Teracom TCW220 dataloggera."""
from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

from settings import (AppConfig, ConfigError, DataloggerConfig, app_dir,
                      default_config_path, load_config, write_sample_config)
from tcw220_client import (DeviceError, Reading, TCW220Client, export_csv,
                           format_number, prepare_readings)

APP_NAME = "TCW220 Monitor"
VERSION = "1.0"

FONT = "Segoe UI"
COL_BG = "#f2f4f7"
COL_CARD = "#ffffff"
COL_BORDER = "#d0d5dd"
COL_TEXT = "#101828"
COL_MUTED = "#667085"
COL_OK = "#2e7d32"
COL_ERR = "#c62828"
COL_PENDING = "#98a2b3"


def setup_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(app_dir(), "tcw220_monitor.log"), maxBytes=500_000,
        backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s")


def client_for(dl: DataloggerConfig, cfg: AppConfig) -> TCW220Client:
    return TCW220Client(dl.host, dl.port, dl.username, dl.password,
                        timeout=cfg.timeout_seconds, https=dl.https)


def safe_name(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_") or "datalogger"


def display_value(r: Reading, decimal: str) -> str:
    text = r.value_text
    if r.value is not None and r.group in ("sensors", "analog"):
        text = text.replace(".", decimal)
        return f"{text} {r.unit}".strip()
    return text


# --------------------------------------------------------------------------- #
# Pozadinska nit koja periodično čita jedan datalogger
# --------------------------------------------------------------------------- #
class Poller(threading.Thread):
    def __init__(self, dl: DataloggerConfig, cfg: AppConfig, out: queue.Queue):
        super().__init__(daemon=True, name=f"poller-{dl.name}")
        self.dl, self.cfg, self.out = dl, cfg, out
        self.client = client_for(dl, cfg)
        self.wake = threading.Event()
        self.stop_flag = threading.Event()

    def run(self) -> None:
        while not self.stop_flag.is_set():
            try:
                snap = self.client.get_status()
                self.out.put((self.dl.name, "ok", snap))
            except DeviceError as exc:
                self.out.put((self.dl.name, "error", str(exc)))
            except Exception as exc:  # nikad ne ruši nit
                logging.exception("Neočekivana greška (%s)", self.dl.name)
                self.out.put((self.dl.name, "error", f"Neočekivana greška: {exc}"))
            self.wake.wait(self.cfg.refresh_seconds)
            self.wake.clear()

    def refresh_now(self) -> None:
        self.wake.set()

    def stop(self) -> None:
        self.stop_flag.set()
        self.wake.set()


# --------------------------------------------------------------------------- #
# Kartica jednog dataloggera
# --------------------------------------------------------------------------- #
class DataloggerCard(tk.Frame):
    def __init__(self, master, dl: DataloggerConfig, decimal: str, on_archive):
        super().__init__(master, bg=COL_CARD, highlightbackground=COL_BORDER,
                         highlightthickness=1, padx=16, pady=12)
        self.dl, self.decimal = dl, decimal
        self._keys: tuple = ()
        self._rows: dict = {}
        self._last_ok: datetime | None = None

        head = tk.Frame(self, bg=COL_CARD)
        head.pack(fill="x")
        self.dot = tk.Label(head, text="●", fg=COL_PENDING, bg=COL_CARD, font=(FONT, 14))
        self.dot.pack(side="left")
        tk.Label(head, text=dl.name, bg=COL_CARD, fg=COL_TEXT,
                 font=(FONT, 13, "bold")).pack(side="left", padx=(6, 0))
        tk.Label(head, text=dl.host, bg=COL_CARD, fg=COL_MUTED,
                 font=(FONT, 9)).pack(side="right")

        self.status = tk.Label(self, text="Povezivanje…", bg=COL_CARD, fg=COL_MUTED,
                               font=(FONT, 9), anchor="w", justify="left", wraplength=380)
        self.status.pack(fill="x", pady=(2, 8))

        self.body = tk.Frame(self, bg=COL_CARD)
        self.body.pack(fill="x")
        self.body.columnconfigure(0, weight=1)

        ttk.Button(self, text="Arhiva…", command=lambda: on_archive(dl.name)
                   ).pack(anchor="e", pady=(10, 0))

    def _rebuild(self, readings: list) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self._rows = {}
        if not readings:
            tk.Label(self.body, text="Nema aktivnih mjerenja za prikaz.", bg=COL_CARD,
                     fg=COL_MUTED, font=(FONT, 10)).grid(row=0, column=0, sticky="w")
        for i, r in enumerate(readings):
            name = tk.Label(self.body, text=r.name, bg=COL_CARD, fg=COL_TEXT,
                            font=(FONT, 11), anchor="w")
            name.grid(row=i, column=0, sticky="w", pady=3)
            val = tk.Label(self.body, text="", bg=COL_CARD, fg=COL_TEXT,
                           font=(FONT, 17, "bold"), anchor="e")
            val.grid(row=i, column=1, sticky="e", padx=(20, 0), pady=3)
            self._rows[r.key] = (name, val)
        self._keys = tuple(r.key for r in readings)

    def update_ok(self, snap, readings: list) -> None:
        if tuple(r.key for r in readings) != self._keys:
            self._rebuild(readings)
        for r in readings:
            name, val = self._rows[r.key]
            name.config(text=r.name)
            val.config(text=display_value(r, self.decimal), fg=COL_ERR if r.alarm else COL_TEXT)
        self._last_ok = snap.fetched_at
        text = f"Online · osvježeno {snap.fetched_at:%H:%M:%S}"
        if snap.device_time:
            text += f" · sat uređaja: {snap.device_time}"
        if snap.hw_error:
            text += f" · greška uređaja: {snap.hw_error}"
        self.dot.config(fg=COL_OK)
        self.status.config(text=text, fg=COL_MUTED)

    def update_error(self, message: str) -> None:
        text = f"OFFLINE · {message}"
        if self._last_ok:
            text += f" (zadnje očitanje {self._last_ok:%H:%M:%S})"
        self.dot.config(fg=COL_ERR)
        self.status.config(text=text, fg=COL_ERR)
        for _, val in self._rows.values():
            val.config(fg=COL_PENDING)       # stare vrijednosti prikazane sivo


# --------------------------------------------------------------------------- #
# Prozor za arhivu
# --------------------------------------------------------------------------- #
class ArchiveWindow(tk.Toplevel):
    DATE_FMT = "%d.%m.%Y"
    MAX_ROWS_SHOWN = 20000

    def __init__(self, app: "App", name: str):
        super().__init__(app.root)
        self.app = app
        self.title("Arhiva podataka")
        self.geometry("1000x640")
        self.minsize(760, 480)
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.logdata = None
        self.chan_vars: dict = {}
        self._after_id = None

        names = [n for n in app.names_in_order() if n in app.latest]
        self.var_name = tk.StringVar(value=name)
        self.var_from = tk.StringVar()
        self.var_to = tk.StringVar()

        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Datalogger:").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(top, textvariable=self.var_name, values=names,
                          state="readonly", width=28)
        cb.grid(row=0, column=1, sticky="w", padx=(6, 24))
        cb.bind("<<ComboboxSelected>>", lambda e: self._rebuild_channels())
        ttk.Label(top, text="Od:").grid(row=0, column=2)
        ttk.Entry(top, textvariable=self.var_from, width=12).grid(row=0, column=3, padx=(4, 12))
        ttk.Label(top, text="Do:").grid(row=0, column=4)
        ttk.Entry(top, textvariable=self.var_to, width=12).grid(row=0, column=5, padx=(4, 12))
        ttk.Label(top, text="(dd.mm.gggg)", foreground=COL_MUTED).grid(row=0, column=6)

        quick = ttk.Frame(self, padding=(12, 0, 12, 4))
        quick.pack(fill="x")
        ttk.Label(quick, text="Brzi izbor:").pack(side="left")
        for label, days in (("Zadnja 24 h", 1), ("7 dana", 7), ("30 dana", 30), ("90 dana", 90)):
            ttk.Button(quick, text=label, command=lambda d=days: self._set_range(d)
                       ).pack(side="left", padx=(6, 0))

        self.chan_frame = ttk.LabelFrame(self, text="Mjerenja", padding=8)
        self.chan_frame.pack(fill="x", padx=12, pady=6)

        bar = ttk.Frame(self, padding=(12, 0, 12, 4))
        bar.pack(fill="x")
        self.btn_load = ttk.Button(bar, text="Učitaj iz uređaja", command=self._start_load)
        self.btn_load.pack(side="left")
        self.btn_cancel = ttk.Button(bar, text="Prekini", command=self.cancel.set, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.btn_save = ttk.Button(bar, text="Spremi u CSV…", command=self._save_csv, state="disabled")
        self.btn_save.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=12, pady=(2, 0))
        self.info = tk.StringVar(value="Odaberite period i kliknite 'Učitaj iz uređaja'.")
        ttk.Label(self, textvariable=self.info, foreground=COL_MUTED,
                  wraplength=950, justify="left").pack(fill="x", padx=12, pady=4)

        frame = ttk.Frame(self, padding=(12, 0, 12, 12))
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, show="headings")
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.tag_configure("alarm", foreground=COL_ERR)

        self._set_range(1)
        self._rebuild_channels()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._after_id = self.after(100, self._poll)

    # -- postavke ---------------------------------------------------------- #
    def _set_range(self, days: int) -> None:
        today = datetime.now()
        self.var_to.set(today.strftime(self.DATE_FMT))
        self.var_from.set((today - timedelta(days=days)).strftime(self.DATE_FMT))

    def _rebuild_channels(self) -> None:
        for child in self.chan_frame.winfo_children():
            child.destroy()
        self.chan_vars = {}
        entry = self.app.latest.get(self.var_name.get())
        readings = [r for r in (entry[1] if entry else []) if r.log_index]
        for i, r in enumerate(readings):
            var = tk.BooleanVar(value=True)
            self.chan_vars[r.log_index] = (var, r)
            ttk.Checkbutton(self.chan_frame, text=r.name + (f" [{r.unit}]" if r.unit else ""),
                            variable=var).grid(row=i // 3, column=i % 3, sticky="w", padx=8, pady=2)
        if not readings:
            ttk.Label(self.chan_frame, text="Nema dostupnih mjerenja.").grid(row=0, column=0)

    def _parse_dates(self):
        try:
            d0 = datetime.strptime(self.var_from.get().strip(), self.DATE_FMT)
            d1 = datetime.strptime(self.var_to.get().strip(), self.DATE_FMT)
        except ValueError:
            raise ValueError("Datum mora biti u obliku dd.mm.gggg, npr. 05.11.2026.")
        if d0 > d1:
            raise ValueError("Datum 'Od' je poslije datuma 'Do'.")
        return d0, d1.replace(hour=23, minute=59, second=59)

    # -- preuzimanje ------------------------------------------------------- #
    def _start_load(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            start, end = self._parse_dates()
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc), parent=self)
            return
        selected = [(idx, r) for idx, (var, r) in self.chan_vars.items() if var.get()]
        if not selected:
            messagebox.showwarning(APP_NAME, "Odaberite barem jedno mjerenje.", parent=self)
            return
        dl = self.app.dl_by_name(self.var_name.get())
        indices = [idx for idx, _ in selected]
        titles = {idx: r.name + (f" [{r.unit}]" if r.unit else "") for idx, r in selected}

        self.cancel.clear()
        self.logdata = None
        self.tree.delete(*self.tree.get_children())
        self.progress.configure(value=0, maximum=1)
        self.info.set("Preuzimanje podataka s uređaja…")
        self.btn_load.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.btn_save.config(state="disabled")
        self.worker = threading.Thread(
            target=self._work, args=(dl, start, end, indices, titles), daemon=True)
        self.worker.start()

    def _work(self, dl, start, end, indices, titles) -> None:
        try:
            client = client_for(dl, self.app.cfg)
            data = client.get_log(
                start, end, indices, titles,
                progress=lambda d, t: self.q.put(("progress", d, t)), cancel=self.cancel)
            self.q.put(("done", data))
        except DeviceError as exc:
            self.q.put(("error", str(exc)))
        except Exception as exc:
            logging.exception("Greška pri preuzimanju loga")
            self.q.put(("error", f"Neočekivana greška: {exc}"))

    def _poll(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    self.progress.configure(maximum=msg[2], value=msg[1])
                    self.info.set(f"Preuzimanje… {msg[1]}/{msg[2]}")
                elif msg[0] == "done":
                    self._show(msg[1])
                elif msg[0] == "error":
                    self._finish_buttons()
                    self.info.set(msg[1])
                    messagebox.showerror(APP_NAME, msg[1], parent=self)
        except queue.Empty:
            pass
        self._after_id = self.after(100, self._poll)

    def _finish_buttons(self) -> None:
        self.btn_load.config(state="normal")
        self.btn_cancel.config(state="disabled")

    def _show(self, data) -> None:
        self._finish_buttons()
        self.logdata = data
        n = len(data.rows)
        if n == 0:
            self.info.set("Nema zapisa u odabranom periodu. Provjerite je li Logger uključen "
                          "na uređaju (SERVICES → Logger) i je li interval snimanja podešen.")
            return
        dec = self.app.cfg.decimal_separator
        cols = ["time"] + [f"c{i}" for i in range(len(data.columns))]
        self.tree.configure(columns=cols)
        self.tree.heading("time", text="Vrijeme")
        self.tree.column("time", width=150, anchor="w", stretch=False)
        for i, c in enumerate(data.columns):
            self.tree.heading(f"c{i}", text=c.title)
            self.tree.column(f"c{i}", width=max(130, 8 * len(c.title)), anchor="e")
        shown = data.rows[-self.MAX_ROWS_SHOWN:]
        for ts, values, alarms in shown:
            cells = [ts.strftime("%d.%m.%Y %H:%M:%S")]
            for c, v in zip(data.columns, values):
                cells.append("—" if v is None else format_number(v, c.digits, dec))
            self.tree.insert("", "end", values=cells, tags=("alarm",) if any(alarms) else ())
        self.tree.yview_moveto(1.0)
        text = (f"Učitano zapisa: {n}  ({data.rows[0][0]:%d.%m.%Y %H:%M} – "
                f"{data.rows[-1][0]:%d.%m.%Y %H:%M})")
        if n > self.MAX_ROWS_SHOWN:
            text += f". Prikazano zadnjih {self.MAX_ROWS_SHOWN}; CSV sadrži sve zapise."
        self.info.set(text)
        self.btn_save.config(state="normal")

    # -- spremanje --------------------------------------------------------- #
    def _save_csv(self) -> None:
        if not self.logdata or not self.logdata.rows:
            return
        folder = self.app.cfg.archive_folder
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            folder = app_dir()
        start = self.var_from.get().replace(".", "-")
        end = self.var_to.get().replace(".", "-")
        path = filedialog.asksaveasfilename(
            parent=self, title="Spremi arhivu", initialdir=folder,
            initialfile=f"{safe_name(self.var_name.get())}_{start}_{end}.csv",
            defaultextension=".csv", filetypes=[("CSV (Excel)", "*.csv"), ("Svi fajlovi", "*.*")])
        if not path:
            return
        try:
            n = export_csv(path, self.logdata, self.app.cfg.decimal_separator)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Fajl nije moguće snimiti:\n{exc}\n\n"
                                 "Ako je fajl otvoren u Excelu, zatvorite ga i pokušajte ponovo.",
                                 parent=self)
            return
        self.info.set(f"Snimljeno {n} zapisa u: {path}")

    def _close(self) -> None:
        self.cancel.set()
        if self._after_id:
            self.after_cancel(self._after_id)
        self.destroy()


# --------------------------------------------------------------------------- #
# Glavni prozor
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root: tk.Tk, cfg: AppConfig):
        self.root, self.cfg = root, cfg
        self.q: queue.Queue = queue.Queue()
        self.latest: dict = {}          # naziv -> (Snapshot, lista Reading)
        self.cards: dict = {}
        self.pollers: dict = {}
        self.state: dict = {}
        self.enabled = [d for d in cfg.dataloggers if d.enabled]

        root.title(f"{APP_NAME} {VERSION}")
        root.configure(bg=COL_BG)
        root.minsize(480, 360)
        root.geometry("{}x{}".format(520 if len(self.enabled) == 1 else 1000, 560))

        style = ttk.Style()
        try:
            style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        except tk.TclError:
            pass

        bar = tk.Frame(root, bg=COL_BG, padx=12, pady=10)
        bar.pack(fill="x")
        ttk.Button(bar, text="Osvježi sad", command=self.refresh_all).pack(side="left")
        ttk.Button(bar, text="Arhiva…", command=self.open_archive_any).pack(side="left", padx=8)
        tk.Label(bar, text=f"Automatsko osvježavanje: svakih {cfg.refresh_seconds} s",
                 bg=COL_BG, fg=COL_MUTED, font=(FONT, 9)).pack(side="right")

        container = tk.Frame(root, bg=COL_BG)
        container.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(container, bg=COL_BG, highlightthickness=0)
        sb = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=COL_BG)
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        ncols = 1 if len(self.enabled) == 1 else 2
        for c in range(ncols):
            self.inner.columnconfigure(c, weight=1, uniform="cards")
        for i, dl in enumerate(self.enabled):
            card = DataloggerCard(self.inner, dl, cfg.decimal_separator, self.open_archive)
            card.grid(row=i // ncols, column=i % ncols, sticky="nsew", padx=(12, 4 if ncols > 1 else 12), pady=6)
            self.cards[dl.name] = card

        self.statusbar = tk.Label(root, text="", anchor="w", bg=COL_BG, fg=COL_MUTED,
                                  font=(FONT, 9), padx=12, pady=6)
        self.statusbar.pack(fill="x")

        for dl in self.enabled:
            p = Poller(dl, cfg, self.q)
            self.pollers[dl.name] = p
            p.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(200, self._drain)

    # -- pomoćne ----------------------------------------------------------- #
    def names_in_order(self) -> list:
        return [d.name for d in self.enabled]

    def dl_by_name(self, name: str) -> DataloggerConfig:
        return next(d for d in self.enabled if d.name == name)

    def _on_wheel(self, event) -> None:
        if self.inner.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    # -- osvježavanje ------------------------------------------------------ #
    def refresh_all(self) -> None:
        for p in self.pollers.values():
            p.refresh_now()

    def _drain(self) -> None:
        try:
            while True:
                name, kind, payload = self.q.get_nowait()
                card = self.cards[name]
                dl = self.dl_by_name(name)
                if kind == "ok":
                    readings = prepare_readings(payload.readings, dl.groups,
                                                dl.hide_channels, dl.channel_names)
                    self.latest[name] = (payload, readings)
                    card.update_ok(payload, readings)
                    if self.state.get(name) != "ok":
                        logging.info("%s: veza uspostavljena", name)
                    self.state[name] = "ok"
                    self.statusbar.config(text=f"Zadnje osvježenje: {payload.fetched_at:%d.%m.%Y %H:%M:%S}")
                    if self.cfg.record_live:
                        self._record_live(dl, readings, payload.fetched_at)
                else:
                    card.update_error(payload)
                    if self.state.get(name) != "error":
                        logging.warning("%s: %s", name, payload)
                    self.state[name] = "error"
        except queue.Empty:
            pass
        self.root.after(200, self._drain)

    def _record_live(self, dl: DataloggerConfig, readings: list, when: datetime) -> None:
        """Dodaj trenutna očitanja u CSV (jedan red po mjerenju)."""
        try:
            os.makedirs(self.cfg.archive_folder, exist_ok=True)
            path = os.path.join(self.cfg.archive_folder, f"uzivo_{safe_name(dl.name)}.csv")
            new = not os.path.exists(path)
            with open(path, "a", encoding="utf-8-sig", newline="") as f:
                if new:
                    f.write("Vrijeme;Mjerenje;Vrijednost;Jedinica\n")
                for r in readings:
                    value = r.value_text.replace(".", self.cfg.decimal_separator) \
                        if r.value is not None else r.value_text
                    f.write(f"{when:%d.%m.%Y %H:%M:%S};{r.name};{value};{r.unit}\n")
        except OSError:
            logging.exception("Snimanje očitanja uživo nije uspjelo")

    # -- arhiva ------------------------------------------------------------ #
    def open_archive(self, name: str) -> None:
        if name not in self.latest:
            messagebox.showinfo(APP_NAME, f"Datalogger '{name}' još nije očitan.\n"
                                "Pričekajte prvo očitanje ili provjerite vezu s uređajem.")
            return
        ArchiveWindow(self, name)

    def open_archive_any(self) -> None:
        for name in self.names_in_order():
            if name in self.latest:
                self.open_archive(name)
                return
        messagebox.showinfo(APP_NAME, "Nijedan datalogger još nije očitan.\n"
                            "Pričekajte prvo očitanje ili provjerite vezu s uređajima.")

    def close(self) -> None:
        for p in self.pollers.values():
            p.stop()
        self.root.destroy()


def main() -> None:
    setup_logging()
    try:  # oštar tekst na ekranima s povećanjem (Windows)
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.withdraw()          # sakrij prazan prozor dok se prikazuju poruke
    path = default_config_path()
    if not os.path.exists(path):
        write_sample_config(path)
        messagebox.showinfo(
            APP_NAME, "Konfiguracijski fajl nije postojao pa je napravljen primjer:\n\n"
            f"{path}\n\nOtvorite ga u Notepadu, upišite podatke svojih dataloggera "
            "i ponovo pokrenite program.")
        return
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        messagebox.showerror(APP_NAME, str(exc))
        return

    logging.info("Pokretanje %s %s, dataloggera: %d", APP_NAME, VERSION, len(cfg.dataloggers))
    root.deiconify()
    App(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()
